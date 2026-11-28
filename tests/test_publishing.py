import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app
from tests.test_drafts import stream
from tests.test_importer import ImportStand


class PublishStand(ImportStand):
    """The tracker can commit a PUT even when the caller never receives its reply."""

    def __init__(self):
        super().__init__()
        self.offers.append({"id": 18003, "name": "Third offer"})
        self.flow = self.streams[41][1]
        self.flow["filters"] = [{"name": "country", "mode": "reject", "payload": ["AU"]}]
        self.published = []
        self.put_outcome = "ok"

    def __call__(self, request):
        if request.method != "PUT":
            return super().__call__(request)
        path = request.url.path.removeprefix("/admin_api/v1")
        if path != "/streams/711":
            raise AssertionError(f"Unexpected PUT to {path}")
        body = json.loads(request.content)
        self.requests.append(("PUT", path, {}))
        self.published.append(body)
        outcome, self.put_outcome = self.put_outcome, "ok"
        if outcome in ("lost_before_commit", "lost_before_then_offline"):
            self.offline = outcome == "lost_before_then_offline"
            raise httpx.WriteTimeout("Unknown whether the PUT reached the tracker")
        self.flow["offers"] = [dict(item, id=1000 + i) for i, item in enumerate(body["offers"])]
        if outcome == "concurrent_write":
            self.flow["offers"] = self.flow["offers"][::-1]
        if outcome in ("lost_after_commit", "lost_then_offline"):
            self.offline = outcome == "lost_then_offline"
            raise httpx.ReadTimeout("Lost reply after the tracker committed")
        return httpx.Response(200, json=self.flow)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{Path(directory).as_posix()}/publish.sqlite3",
            "KEITARO_BASE_URL": "https://stand.example/admin_api/v1",
            "KEITARO_API_KEY": "test-fixture",
        }))
        command.upgrade(Config("alembic.ini"), "head")
        self.stand = PublishStand()
        http_client = httpx.AsyncClient
        self.enterContext(patch("app.main.httpx.AsyncClient", side_effect=lambda **kwargs:
                                http_client(transport=httpx.MockTransport(self.stand), **kwargs)))
        self.server = self.enterContext(ExitStack())
        self.start_app()
        self.assertEqual(self.client.post("/api/campaigns/41/fetch", json={}).status_code, 200)
        self.stand.requests.clear()

    def start_app(self):
        self.client = self.server.enter_context(TestClient(app, base_url="http://localhost"))

    def local(self, sid=711):
        reply = self.client.get("/api/campaigns/41/streams")
        self.assertEqual(reply.status_code, 200, reply.text)
        return stream(reply.json(), sid)

    def edit(self, action, offer_id, sid=711, **kwargs):
        payload = dict(action=action, expected_revision=self.local(sid)["revision"], **kwargs)
        if offer_id is not None:
            payload["offer_id"] = offer_id
        reply = self.client.post(f"/api/campaigns/41/streams/{sid}/draft", json=payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        return stream(reply.json(), sid)

    def push(self, sid=711, *, revision=None):
        if revision is None:
            revision = self.local(sid)["revision"]
        return self.client.post(f"/api/campaigns/41/streams/{sid}/push", json={"expected_revision": revision})

    def accepted(self):
        reply = self.push()
        self.assertEqual(reply.status_code, 200, reply.text)
        result = stream(reply.json())
        self.assertFalse(result["has_draft"])
        self.assertFalse(result["push_pending"])
        self.assertEqual(result, self.local())
        return result

    def test_put_sends_only_selected_offers_and_verifies_remote_state(self):
        neighbor = self.edit("add", 18003, sid=712)
        original_neighbor = deepcopy(self.stand.streams[41][2])
        filters = deepcopy(self.stand.flow["filters"])
        self.edit("add", 18003)
        draft = self.edit("pin", 18001, share=33)
        self.stand.requests.clear()
        confirmed = self.accepted()
        self.assertEqual([(method, path) for method, path, _ in self.stand.requests],
                         [("GET", "/streams/711"), ("PUT", "/streams/711"), ("GET", "/streams/711")])
        self.assertEqual(self.stand.published, [{"offers": [
            {"offer_id": 18002, "share": 27, "state": "disabled"},
            {"offer_id": 18001, "share": 33, "state": "active"},
            {"offer_id": 18003, "share": 67, "state": "active"},
        ]}])
        self.assertEqual(confirmed["offers"], draft["offers"])
        self.assertEqual(self.stand.flow["filters"], filters)
        self.assertEqual(self.stand.streams[41][2], original_neighbor)
        self.assertEqual(self.local(712), neighbor)
        self.assertEqual(self.push(revision=draft["revision"]).status_code, 409)
        self.assertEqual(len(self.stand.published), 1)

    def test_fetch_preserves_archive_slots_after_push_and_accepts_remote_reordering(self):
        self.edit("add", 18003)
        self.edit("remove", 18002)
        self.edit("remove", 18001)
        published = self.accepted()

        fetched = self.client.post("/api/campaigns/41/fetch", json={})
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(stream(fetched.json()), published)  # Includes the stream revision.

        self.edit("restore", 18001)
        restored = self.edit("restore", 18002)
        self.assertEqual([row["offer_id"] for row in restored["offers"]], [18002, 18001, 18003])
        self.accepted()
        self.stand.flow["offers"].reverse()
        reordered = self.client.post("/api/campaigns/41/fetch", json={})
        self.assertEqual(reordered.status_code, 200, reordered.text)
        self.assertEqual([row["offer_id"] for row in stream(reordered.json())["offers"]], [18003, 18001, 18002])

    def test_preflight_detects_changes_to_order_membership_weight_and_state(self):
        original = deepcopy(self.stand.flow["offers"])
        draft = self.edit("add", 18003)
        variant_offers = {
            "order": original[::-1], "membership": original[:1],
            "share": [dict(original[0], share=22), original[1]],
            "state": [dict(original[0], state="active"), original[1]],
        }
        for field, altered in variant_offers.items():
            with self.subTest(field=field):
                self.stand.flow["offers"] = altered
                self.assertEqual(self.push().status_code, 409)
                self.assertEqual(self.local()["offers"], draft["offers"])
                self.assertTrue(self.local()["has_draft"])
                self.assertFalse(self.local()["push_pending"])
        self.assertEqual(self.stand.published, [])

    def test_lost_reply_is_reconciled_without_repeated_put(self):
        draft = self.edit("add", 18003)
        self.stand.put_outcome = "lost_after_commit"
        self.assertEqual(self.accepted()["offers"], draft["offers"])
        self.assertEqual(len(self.stand.published), 1)

    def test_pending_push_survives_restart_and_blocks_cancel_and_discard(self):
        draft = self.edit("add", 18003)
        self.stand.put_outcome = "lost_then_offline"
        self.assertNotEqual(self.push().status_code, 200)
        self.server.close()
        self.start_app()
        pending = self.local()
        self.assertTrue(pending["has_draft"] and pending["push_pending"])
        self.assertEqual(pending["offers"], draft["offers"])
        payload = {"action": "cancel", "expected_revision": pending["revision"]}
        self.assertEqual(self.client.post("/api/campaigns/41/streams/711/draft", json=payload).status_code, 409)
        self.assertEqual(self.client.post("/api/campaigns/41/fetch", json={
            "discard_drafts": {"711": pending["revision"]},
        }).status_code, 409)
        self.stand.offline = False
        self.assertEqual(self.accepted()["offers"], draft["offers"])
        self.assertEqual(len(self.stand.published), 1)

    def test_ambiguous_write_with_unchanged_remote_requires_explicit_retry(self):
        draft = self.edit("add", 18003)
        self.stand.put_outcome = "lost_before_commit"
        failed = self.push()
        self.assertNotEqual(failed.status_code, 200)
        self.assertEqual(self.local()["offers"], draft["offers"])
        self.assertTrue(self.local()["has_draft"])
        self.assertEqual(len(self.stand.published), 1)
        self.assertEqual(self.accepted()["offers"], draft["offers"])
        self.assertEqual(len(self.stand.published), 2)

    def test_readback_conflict_keeps_draft(self):
        draft = self.edit("add", 18003)
        self.stand.put_outcome = "concurrent_write"
        result = self.push()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertTrue(self.local()["has_draft"])
        self.assertFalse(self.local()["push_pending"])
        self.assertEqual(self.local()["offers"], draft["offers"])
        self.assertEqual(len(self.stand.published), 1)

    def test_recovery_checks_old_baseline_and_stale_recovery_cannot_become_put(self):
        self.edit("add", 18003)
        self.stand.put_outcome = "lost_before_then_offline"
        self.assertNotEqual(self.push().status_code, 200)
        pending = self.local()
        self.assertTrue(pending["push_pending"])
        self.stand.offline = False
        self.stand.requests.clear()
        recovered = self.push(revision=pending["revision"])
        self.assertEqual(recovered.status_code, 409, recovered.text)
        retryable = self.local()
        self.assertFalse(retryable["push_pending"])
        self.assertTrue(retryable["has_draft"])
        self.assertNotEqual(retryable["revision"], pending["revision"])
        self.assertEqual(self.push(revision=pending["revision"]).status_code, 409)
        self.assertEqual(self.stand.requests, [("GET", "/streams/711", {})])
        self.assertEqual(len(self.stand.published), 1)
        self.accepted()
        self.assertEqual(len(self.stand.published), 2)

    def test_pin_only_commit_reads_remote_and_survives_fetch_without_put(self):
        self.edit("pin", 18001, sid=712)
        self.stand.requests.clear()
        reply = self.push(sid=712)
        self.assertEqual(reply.status_code, 200, reply.text)
        flow = stream(reply.json(), 712)
        self.assertFalse(flow["has_draft"])
        self.assertTrue(flow["offers"][0]["pinned"])
        self.assertEqual(self.stand.requests, [("GET", "/streams/712", {})])
        self.assertEqual(self.client.post("/api/campaigns/41/fetch", json={}).status_code, 200)
        self.assertTrue(self.local(712)["offers"][0]["pinned"])

    def test_sparse_shares_and_restore_of_archived_offer(self):
        self.edit("remove", 18002)
        removed = self.accepted()
        self.assertEqual(self.stand.published[-1], {"offers": [{"offer_id": 18001, "share": 73, "state": "active"}]})
        archived = next(o for o in removed["offers"] if o["offer_id"] == 18002)
        self.assertTrue(archived["removed"])
        self.assertEqual(archived["share"], 0)
        self.edit("restore", 18002)
        restored = self.accepted()
        self.assertEqual(self.stand.published[-1], {"offers": [
            {"offer_id": 18002, "share": 50, "state": "active"}, {"offer_id": 18001, "share": 50, "state": "active"},
        ]})
        self.assertFalse(any(row["removed"] for row in restored["offers"]))

    def test_empty_offer_list_is_published_and_archive_is_retained(self):
        for o in self.stand.flow["offers"]:
            o["state"] = "disabled"
        self.assertEqual(self.client.post("/api/campaigns/41/fetch", json={}).status_code, 200)
        for offer_id in (18001, 18002):
            self.edit("remove", offer_id)
        result = self.accepted()
        self.assertEqual(self.stand.published, [{"offers": []}])
        self.assertEqual({o["offer_id"] for o in result["offers"] if o["removed"]}, {18001, 18002})

    def test_concurrent_push_of_same_draft_sends_only_one_put(self):
        revision = self.edit("add", 18003)["revision"]
        entered, release = Event(), Event()
        upstream = self.client.app.state.keitaro
        write = upstream.put_stream_offers

        async def paused_write(*args, **kwargs):
            entered.set()
            if not await asyncio.to_thread(release.wait, 5):
                raise AssertionError("Timed out waiting for the contending Push")
            return await write(*args, **kwargs)

        with patch.object(upstream, "put_stream_offers", paused_write), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.push, revision=revision)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.push(revision=revision).status_code, 409)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)
        self.assertEqual(len(self.stand.published), 1)
