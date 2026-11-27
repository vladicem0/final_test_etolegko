import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app


class FakeKeitaro:
    """Small stateful tracker; a POST may commit even when its reply is lost."""

    def __init__(self):
        self.campaigns = {}
        self.streams = {}
        self.reads = []
        self.writes = []
        self.fail_before_write = {}
        self.lose_reply_after = None
        self.offline = False

    def __call__(self, request):
        path = request.url.path.removeprefix("/admin_api/v1")
        if self.offline:
            raise httpx.ConnectError("Temporary outage")
        if request.method == "GET":
            self.reads.append(path)
            catalogs = {
                "/domains": [{"id": 1813, "name": "domain.example"}],
                "/groups": [{"id": 10332, "name": "FORTESTS"}],
                "/traffic_sources": [{"id": 836, "name": "FORTESTS"}],
                "/offers/18001": {"id": 18001, "name": "Offer", "state": "active"},
            }
            if path == "/groups" and request.url.params.get("type") != "campaigns":
                raise AssertionError("Wrong group type")
            if path in catalogs:
                return httpx.Response(200, json=catalogs[path])
            if path == "/campaigns":
                offset = int(request.url.params.get("offset", 0))
                limit = int(request.url.params.get("limit", 100))
                rows = list(self.campaigns.values())[offset:offset + limit]
                return httpx.Response(200, json=rows)
            if path.startswith("/campaigns/"):
                campaign_id = int(path.split("/")[2])
                if campaign_id in self.campaigns:
                    if path.endswith("/streams"):
                        return httpx.Response(200, json=self.streams[campaign_id])
                    return httpx.Response(200, json=self.campaigns[campaign_id])
            return httpx.Response(404, json={"error": "Unknown record"})

        if request.method != "POST":
            raise AssertionError(f"Unexpected remote write: {request.method} {path}")
        failure = self.fail_before_write.pop(len(self.writes) + 1, None)
        if failure is not None:
            raise failure
        body = json.loads(request.content)
        self.writes.append((path, body))
        if path == "/campaigns":
            campaign_id = len(self.campaigns) + 1
            row = dict(body, id=campaign_id, group_id=int(body["group_id"]), state="active")
            self.campaigns[campaign_id] = row
            self.streams[campaign_id] = []
        elif path == "/streams":
            siblings = self.streams[body["campaign_id"]]
            row = {"filters": [], "offers": [], "state": "active", "position": len(siblings) + 1}
            row.update(body, id=len(siblings) + 100)
            siblings.append(row)
        else:
            raise AssertionError(f"Unexpected remote POST {path}")
        if len(self.writes) == self.lose_reply_after:
            self.offline = True
            raise httpx.ReadTimeout("Response lost after commit")
        return httpx.Response(200, json=row)


class CreatorTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{Path(directory).as_posix()}/creator.sqlite3",
            "KEITARO_BASE_URL": "https://stand.example/admin_api/v1",
            "KEITARO_API_KEY": "test-fixture",
        }))
        command.upgrade(Config("alembic.ini"), "head")
        self.stand = FakeKeitaro()
        real_client = httpx.AsyncClient
        self.enterContext(patch(
            "app.main.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(self.stand), **kwargs),
        ))
        self.client = self.enterContext(TestClient(app, base_url="http://localhost"))
        self.fields = {
            "request_id": str(uuid4()), "name": "sample", "geo_code": "au", "offer_id": 18001,
        }

    def submit(self, **changes):
        return self.client.post("/api/campaigns", json=self.fields | changes)

    def assert_created(self, response):
        self.assertIn(response.status_code, (200, 201), response.text)
        data = response.json()
        self.assertEqual(data["name"], "test_nitsevich_sample")
        self.assertEqual(data["campaign_id"], 1)
        self.assertEqual(len(data["streams"]), 2)
        return data

    def test_form_and_validation(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<form", page.text.lower())
        for changes in ({"geo_code": "AUS"}, {"name": " "}, {"offer_id": 0}, {"offer_id": 99999}):
            with self.subTest(changes=changes):
                self.assertEqual(self.submit(**changes).status_code, 422)
        self.assertFalse(self.stand.writes)

    def test_create_verify_and_repeat(self):
        self.fields["name"] = "test_nitsevich_sample"
        created = self.assert_created(self.submit())
        self.assertEqual([p for p, _ in self.stand.writes], ["/campaigns", "/streams", "/streams"])
        campaign, geo, offers = [body for _, body in self.stand.writes]
        self.assertEqual(campaign["alias"], created["alias"])
        self.assertEqual(campaign["type"], "position")
        self.assertEqual(int(campaign["group_id"]), 10332)
        self.assertEqual(campaign["domain_id"], 1813)
        self.assertEqual(campaign["traffic_source_id"], 836)
        self.assertEqual((geo["type"], geo["schema"], geo["action_type"]), ("regular", "redirect", "http"))
        self.assertEqual(geo["action_payload"].rstrip("/"), "https://google.com")
        self.assertEqual(geo["filters"], [{"name": "country", "mode": "accept", "payload": ["AU"]}])
        self.assertEqual((offers["type"], offers["schema"]), ("default", "landings"))
        self.assertEqual(offers["offers"], [{"offer_id": 18001, "share": 100, "state": "active"}])
        self.assertTrue({
            "/domains", "/groups", "/traffic_sources", "/offers/18001",
            "/campaigns/1", "/campaigns/1/streams",
        }.issubset(self.stand.reads))
        self.assertEqual(self.assert_created(self.submit())["alias"], created["alias"])
        self.assertEqual(self.submit(name="different").status_code, 409)
        self.assertEqual(len(self.stand.writes), 3)

    def test_foreign_origin_cannot_create_campaign(self):
        response = self.client.post(
            "/api/campaigns", json=self.fields, headers={"Origin": "https://unrelated.example"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stand.writes, [])

    def test_concurrent_submit_with_same_uuid(self):
        entered = Event()
        release = Event()
        upstream = self.client.app.state.keitaro
        create = upstream.create_campaign

        async def paused_create(payload):
            entered.set()
            if not await asyncio.to_thread(release.wait, 10):
                raise AssertionError("Timed out waiting for the competing request")
            return await create(payload)

        with patch.object(upstream, "create_campaign", paused_create), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.submit)
            try:
                self.assertTrue(entered.wait(5))
                duplicate = self.submit()
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
            finally:
                release.set()
            self.assert_created(first.result(timeout=10))
        self.assert_created(self.submit())
        self.assertEqual(len(self.stand.writes), 3)

    def lose_reply_and_restore_connection(self, after):
        self.stand.lose_reply_after = after
        failed = self.submit()
        self.assertGreaterEqual(failed.status_code, 400, failed.text)
        self.assertEqual(len(self.stand.writes), after)
        self.stand.offline = False

    def test_retry_each_unsent_post_on_next_submission(self):
        self.stand.fail_before_write = {
            1: httpx.ConnectError("Could not connect"),
            2: httpx.ConnectTimeout("Connection timed out"),
            3: httpx.PoolTimeout("Connection pool busy"),
        }
        for succeeded, expected_status in enumerate([502, 504, 504]):
            with self.subTest(completed_posts=succeeded):
                response = self.submit()
                self.assertEqual(response.status_code, expected_status, response.text)
                self.assertEqual(len(self.stand.writes), succeeded)
        self.assert_created(self.submit())
        self.assert_created(self.submit())
        self.assertEqual([path for path, _ in self.stand.writes], ["/campaigns", "/streams", "/streams"])

    def test_stream_write_timeout_does_not_authorize_another_post(self):
        self.stand.fail_before_write[2] = httpx.WriteTimeout("Unknown delivery outcome")
        self.assertEqual(self.submit().status_code, 504)
        self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(len(self.stand.writes), 1)

    def test_recover_committed_campaign_after_lost_response(self):
        self.lose_reply_and_restore_connection(1)
        self.assert_created(self.submit())
        self.assertEqual(len(self.stand.writes), 3)
        self.assertEqual(len(self.stand.campaigns), 1)

    def test_recover_committed_stream_after_lost_response(self):
        self.lose_reply_and_restore_connection(2)
        self.assert_created(self.submit())
        self.assertEqual(len(self.stand.writes), 3)
        self.assertEqual(len(self.stand.streams[1]), 2)

    def test_ambiguous_missing_campaign_is_not_reposted(self):
        self.lose_reply_and_restore_connection(1)
        self.stand.campaigns.clear()
        self.assertGreaterEqual(self.submit().status_code, 400)
        self.assertEqual(len(self.stand.writes), 1)

    def test_external_change_blocks_recovery(self):
        self.lose_reply_and_restore_connection(2)
        self.stand.streams[1][0]["action_payload"] = "https://example.com/"
        self.assertGreaterEqual(self.submit().status_code, 400)
        self.assertEqual(len(self.stand.writes), 2)
