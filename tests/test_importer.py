import asyncio
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

from sqlalchemy.orm import Session

from app.main import app
from app.models import Campaign
from tests.test_creator import FakeKeitaro


def binding(row_id, offer_id, share, state="active"):
    return {"id": row_id, "offer_id": offer_id, "share": share, "state": state}


class ImportStand(FakeKeitaro):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.offers = [
            {"id": 18001, "name": "Original offer"},
            {"id": 18002, "name": "Second offer"},
        ]
        self.campaigns = {
            41: {"id": 41, "name": "External campaign", "alias": "external-campaign"},
            52: {"id": 52, "name": "Other campaign", "alias": "another-campaign"},
        }
        self.streams = {
            41: [
                {"id": 710, "campaign_id": 41, "position": 1, "name": "Geo", "type": "regular",
                 "schema": "redirect", "offers": []},
                {"id": 711, "campaign_id": 41, "position": 2, "name": "Offers", "type": "default",
                 "schema": "landings", "offers": [
                     binding(20, 18002, 27, "disabled"), binding(21, 18001, 73),
                 ]},
                {"id": 712, "campaign_id": 41, "position": 3, "name": "Another flow", "type": "regular",
                 "schema": "landings", "offers": [binding(22, 18001, 100)]},
            ],
            52: [],
        }

    def __call__(self, request):
        path = request.url.path.removeprefix("/admin_api/v1")
        self.requests.append((request.method, path, dict(request.url.params)))
        if not self.offline and request.method == "GET":
            if path == "/offers":
                return httpx.Response(200, json=self.offers)
            if path.startswith("/offers/"):
                for offer in self.offers:
                    if path == f"/offers/{offer['id']}":
                        return httpx.Response(200, json=offer)
            if path.startswith("/streams/"):
                for streams in self.streams.values():
                    for stream in streams:
                        if path == f"/streams/{stream['id']}":
                            return httpx.Response(200, json=stream)
        return super().__call__(request)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{Path(directory).as_posix()}/import.sqlite3",
            "KEITARO_BASE_URL": "https://stand.example/admin_api/v1",
            "KEITARO_API_KEY": "test-fixture",
        }))
        command.upgrade(Config("alembic.ini"), "head")
        self.stand = ImportStand()
        real_client = httpx.AsyncClient
        self.enterContext(patch(
            "app.main.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(self.stand), **kwargs),
        ))
        self.client = self.enterContext(TestClient(app, base_url="http://localhost"))

    def fetch(self, campaign_id=41):
        response = self.client.post(f"/api/campaigns/{campaign_id}/fetch", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_campaign_listing_uses_requested_page(self):
        response = self.client.get("/api/campaigns", params={"offset": 1, "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), [{"id": 52, "name": "Other campaign"}])
        self.assertEqual(self.stand.requests, [("GET", "/campaigns", {"offset": "1", "limit": "1"})])

    def test_external_campaign_fetch_and_offline_snapshot(self):
        self.assertEqual(self.client.get("/api/campaigns/41/streams").status_code, 404)
        snapshot = self.fetch()
        self.assertEqual((snapshot["id"], snapshot["name"], snapshot["alias"]),
                         (41, "External campaign", "external-campaign"))
        self.assertEqual(snapshot["admin_url"], "https://stand.example/admin/#!/campaign.editor/41")
        self.assertEqual([s["id"] for s in snapshot["streams"]], [710, 711, 712])
        redirect, offers, other_flow = snapshot["streams"]
        self.assertEqual((redirect["name"], redirect["position"], redirect["type"],
                          redirect["schema"], redirect["offers"]), ("Geo", 1, "regular", "redirect", []))
        self.assertEqual(
            [(o["offer_id"], o["name"], o["share"], o["state"], o["removed"]) for o in offers["offers"]],
            [(18002, "Second offer", 27, "disabled", False), (18001, "Original offer", 73, "active", False)],
        )
        self.assertEqual(other_flow["offers"][0]["share"], 100)
        self.assertEqual(self.fetch(), snapshot)
        self.assertTrue(all(method == "GET" for method, _, _ in self.stand.requests))
        self.stand.requests.clear()
        self.stand.offline = True
        local = self.client.get("/api/campaigns/41/streams")
        self.assertEqual(local.status_code, 200)
        self.assertEqual(local.json(), snapshot)
        self.assertEqual(self.stand.requests, [])

    def test_archive_and_reappearance_are_per_stream_and_use_offer_id(self):
        initial = self.fetch()
        remote_offers = self.stand.streams[41][1]["offers"]
        remote_offers.pop()
        archived = self.fetch()
        rows = {offer["offer_id"]: offer for offer in archived["streams"][1]["offers"]}
        self.assertEqual(set(rows), {18001, 18002})
        self.assertTrue(rows[18001]["removed"])
        self.assertFalse(rows[18002]["removed"])
        self.assertEqual(rows[18002]["share"], 27)
        self.assertEqual(archived["streams"][2], initial["streams"][2])

        # Keitaro assigned a new binding ID for the same offer.
        remote_offers.append(binding(99, 18001, 61))
        restored = self.fetch()
        self.assertEqual([(o["offer_id"], o["share"], o["removed"]) for o in restored["streams"][1]["offers"]],
                         [(18002, 27, False), (18001, 61, False)])
        self.assertEqual(restored["streams"][2], initial["streams"][2])

    def test_disappeared_stream_is_hidden_and_retains_its_offer_history(self):
        initial = self.fetch()
        stand_streams = self.stand.streams[41]
        missing = stand_streams.pop(1)
        while_missing = self.fetch()
        self.assertEqual([s["id"] for s in while_missing["streams"]], [710, 712])
        self.assertEqual(while_missing["streams"][1], initial["streams"][2])

        missing["offers"] = [binding(42, 18001, 100)]
        stand_streams.insert(1, missing)
        returned = self.fetch()
        self.assertEqual([s["id"] for s in returned["streams"]], [710, 711, 712])
        rows = {offer["offer_id"]: offer for offer in returned["streams"][1]["offers"]}
        self.assertEqual(set(rows), {18001, 18002})
        self.assertFalse(rows[18001]["removed"])
        self.assertEqual(rows[18001]["share"], 100)
        self.assertTrue(rows[18002]["removed"])
        self.assertEqual(rows[18002]["name"], "Second offer")
        self.assertEqual(rows[18002]["share"], 0)

    def test_older_fetch_cannot_overwrite_concurrent_snapshot(self):
        self.fetch()
        captured = Event()
        release = Event()
        upstream = self.client.app.state.keitaro
        read_streams = upstream.campaign_streams

        async def pause_first_result(campaign_id):
            streams = await read_streams(campaign_id)
            if not captured.is_set():
                captured.set()
                if not await asyncio.to_thread(release.wait, 10):
                    raise AssertionError("Timed out waiting for the newer Fetch")
            return streams

        with patch.object(upstream, "campaign_streams", pause_first_result), ThreadPoolExecutor(1) as pool:
            stale = pool.submit(self.client.post, "/api/campaigns/41/fetch", json={})
            try:
                self.assertTrue(captured.wait(5))
                self.stand.streams[41][1]["offers"][1]["share"] = 51
                fresh = self.fetch()
            finally:
                release.set()
            self.assertEqual(stale.result(timeout=10).status_code, 409)
        self.assertEqual(fresh["streams"][1]["offers"][1]["share"], 51)
        self.assertEqual(self.client.get("/api/campaigns/41/streams").json(), fresh)

    def test_invalid_upstream_stream_leaves_existing_snapshot_intact(self):
        previous = self.fetch()
        self.stand.campaigns[41]["name"] = "New tracker name"
        self.stand.streams[41][1]["offers"][0]["share"] = 48
        del self.stand.streams[41][2]["offers"][0]["offer_id"]
        response = self.client.post("/api/campaigns/41/fetch", json={})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.client.get("/api/campaigns/41/streams").json(), previous)

    def test_pending_creator_alias_blocks_import_until_resolved(self):
        previous = self.fetch(41)
        alias = "kt-" + uuid4().hex
        with Session(self.client.app.state.engine) as db, db.begin():
            db.add(Campaign(name="Pending creator", alias=alias, creation_state="sending_campaign"))
        for campaign_id in (41, 52):
            with self.subTest(campaign_id=campaign_id):
                self.stand.campaigns[campaign_id]["alias"] = alias
                response = self.client.post(f"/api/campaigns/{campaign_id}/fetch", json={})
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get("/api/campaigns/41/streams").json(), previous)
        self.assertEqual(self.client.get("/api/campaigns/52/streams").status_code, 404)
        self.stand.campaigns[52]["alias"] = "resolved-on-stand"
        self.assertEqual(self.fetch(52)["alias"], "resolved-on-stand")

    def test_imported_alias_cannot_be_claimed_by_creator(self):
        request_id = uuid4()
        self.stand.campaigns[41].update(
            alias="kt-" + request_id.hex, name="test_nitsevich_matching", type="position",
            domain_id=1813, group_id="10332", traffic_source_id=836,
        )
        previous = self.fetch(41)
        self.stand.requests.clear()
        response = self.client.post("/api/campaigns", json={
            "request_id": str(request_id), "name": "matching", "geo_code": "AU", "offer_id": 18001,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("#41", response.json()["detail"])
        self.assertFalse(self.stand.requests)
        self.assertEqual(self.fetch(41), previous)

        # The alias can also change in the tracker before the next Fetch.
        renamed_request = uuid4()
        self.stand.campaigns[41]["alias"] = "kt-" + renamed_request.hex
        response = self.client.post("/api/campaigns", json={
            "request_id": str(renamed_request), "name": "matching", "geo_code": "AU", "offer_id": 18001,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("#41", response.json()["detail"])
        self.assertFalse(self.stand.writes)
        self.assertEqual(self.fetch(41)["alias"], "kt-" + renamed_request.hex)

    def test_invalid_campaign_id_is_rejected_before_database_access(self):
        self.assertEqual(self.client.get(f"/api/campaigns/{2**63}/streams").status_code, 422)
        self.assertFalse(self.stand.requests)

    def test_import_campaign_created_with_wrapper(self):
        created = self.client.post("/api/campaigns", json={
            "request_id": str(uuid4()), "name": "wrapper", "geo_code": "AU", "offer_id": 18001,
        })
        self.assertIn(created.status_code, (200, 201), created.text)
        campaign = created.json()
        snapshot = self.fetch(campaign["campaign_id"])
        self.assertEqual(snapshot["alias"], campaign["alias"])
        self.assertEqual([s["id"] for s in snapshot["streams"]], [s["id"] for s in campaign["streams"]])
        self.assertEqual(snapshot["streams"][1]["offers"][0]["offer_id"], 18001)
        self.assertEqual(len(self.stand.writes), 3)

    def test_foreign_origin_cannot_fetch(self):
        response = self.client.post("/api/campaigns/41/fetch", json={},
                                    headers={"Origin": "https://unrelated.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stand.requests, [])
