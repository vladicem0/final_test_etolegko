import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Campaign, Stream, StreamOffer
from tests.test_importer import ImportStand, binding


def stream(snapshot, stream_id=711):
    return next(item for item in snapshot["streams"] if item["id"] == stream_id)


def active_shares(flow):
    return [(row["offer_id"], row["share"]) for row in flow["offers"]
            if not row["removed"] and row["state"] == "active"]


class DraftTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{Path(directory).as_posix()}/drafts.sqlite3",
            "KEITARO_BASE_URL": "https://stand.example/admin_api/v1",
            "KEITARO_API_KEY": "test-fixture",
        }))
        command.upgrade(Config("alembic.ini"), "head")
        self.stand = ImportStand()
        self.stand.offers.extend([{"id": 18003, "name": "Third"}, {"id": 18004, "name": "Fourth"}])
        self.remote_offers = self.stand.streams[41][1]["offers"]
        self.remote_offers[:] = [binding(31, 18001, 12), binding(32, 18002, 18), binding(33, 18003, 70)]
        http_client = httpx.AsyncClient
        self.enterContext(patch("app.main.httpx.AsyncClient", side_effect=lambda **kwargs:
                                http_client(transport=httpx.MockTransport(self.stand), **kwargs)))
        self.client = self.enterContext(TestClient(app, base_url="http://localhost"))
        self.initial = self.fetch()
        self.stand.requests.clear()

    def tearDown(self):
        self.assertTrue(all(method == "GET" for method, _, _ in self.stand.requests),
                        "A local draft operation wrote to the tracker")

    def snapshot(self):
        response = self.client.get("/api/campaigns/41/streams")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def fetch(self, **data):
        response = self.client.post("/api/campaigns/41/fetch", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post_action(self, action, offer_id=None, *, stream_id=711, revision=None, **fields):
        if revision is None:
            revision = stream(self.snapshot(), stream_id)["revision"]
        data = {"action": action, "expected_revision": revision, **fields}
        if offer_id is not None:
            data["offer_id"] = offer_id
        return self.client.post(f"/api/campaigns/41/streams/{stream_id}/draft", json=data)

    def edit(self, action, offer_id=None, *, stream_id=711, **fields):
        response = self.post_action(action, offer_id, stream_id=stream_id, **fields)
        self.assertEqual(response.status_code, 200, response.text)
        return stream(response.json(), stream_id)

    def test_video_weights_local_reload_and_cancel(self):
        draft = self.edit("add", 18004)
        self.assertTrue(draft["has_draft"])
        self.assertEqual(active_shares(draft), [(18001, 25), (18002, 25), (18003, 25), (18004, 25)])
        self.assertTrue(any(path == "/offers/18004" for _, path, _ in self.stand.requests))
        self.stand.offline = True
        self.stand.requests.clear()
        self.edit("pin", 18001)
        draft = self.edit("remove", 18004)
        self.assertEqual(active_shares(draft), [(18001, 25), (18002, 37), (18003, 38)])
        self.assertTrue(draft["offers"][0]["pinned"])
        with Session(self.client.app.state.engine) as db:
            base = db.get(Stream, 711).draft_base
            self.assertEqual([(o["offer_id"], o["share"], o["state"]) for o in base],
                             [(18001, 12, "active"), (18002, 18, "active"), (18003, 70, "active")])
            published = db.scalars(select(StreamOffer).where(StreamOffer.stream_id == 711)
                                   .order_by(StreamOffer.position)).all()
            self.assertEqual([(o.offer_id, o.share, o.pinned) for o in published],
                             [(18001, 12, False), (18002, 18, False), (18003, 70, False)])
        saved = self.snapshot()
        self.assertEqual(stream(saved), draft)
        self.assertEqual(stream(saved, 712), stream(self.initial, 712))
        cancelled = self.edit("cancel")
        self.assertFalse(cancelled["has_draft"])
        self.assertEqual(cancelled["offers"], stream(self.initial)["offers"])
        self.assertEqual(self.stand.requests, [])

    def test_impossible_allocation_and_removing_last_active_leave_state_intact(self):
        self.edit("pin", 18001, share=98)
        pinned = self.snapshot()
        self.assertEqual(active_shares(stream(pinned)), [(18001, 98), (18002, 1), (18003, 1)])
        self.assertIn(self.post_action("add", 18004).status_code, (409, 422))
        self.assertEqual(self.snapshot(), pinned)
        self.assertIn(self.post_action("remove", 18001, stream_id=712).status_code, (409, 422))
        self.assertEqual(self.snapshot(), pinned)

    def test_remove_restore_and_new_binding_inverse(self):
        base = stream(self.initial)
        self.edit("add", 18004)
        undone = self.edit("remove", 18004)
        self.assertFalse(undone["has_draft"])
        self.assertEqual(undone["offers"], base["offers"])

        # External removal archives an existing binding, which can be brought back locally.
        self.remote_offers.pop(1)
        archived = stream(self.fetch())
        row = next(o for o in archived["offers"] if o["offer_id"] == 18002)
        self.assertEqual((row["removed"], row["share"]), (True, 0))
        restored = self.edit("restore", 18002)
        active = next(o for o in restored["offers"] if o["offer_id"] == 18002)
        self.assertEqual((active["removed"], active["pinned"], active["state"]), (False, False, "active"))
        self.assertEqual(sum(share for _, share in active_shares(restored)), 100)
        with Session(self.client.app.state.engine) as db:
            self.assertEqual([row["offer_id"] for row in db.get(Stream, 711).draft_base], [18001, 18003])
        self.assertEqual(self.edit("cancel")["offers"], archived["offers"])

    def test_redundant_unpin_keeps_revision_and_add_undo(self):
        added = self.edit("add", 18004)
        rejected = self.post_action("unpin", 18001, revision=added["revision"])
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(stream(self.snapshot()), added)
        undone = self.edit("remove", 18004, revision=added["revision"])
        self.assertFalse(undone["has_draft"])
        self.assertEqual(undone["offers"], stream(self.initial)["offers"])

    def test_remove_disabled_preserves_active_weights_until_brought_back(self):
        self.remote_offers.append(binding(34, 18004, 9, "disabled"))
        published = stream(self.fetch())
        removed = self.edit("remove", 18004)
        self.assertEqual(active_shares(removed), active_shares(published))
        archived = next(row for row in removed["offers"] if row["offer_id"] == 18004)
        self.assertEqual((archived["removed"], archived["share"]), (True, 0))
        restored = self.edit("restore", 18004)
        self.assertEqual(active_shares(restored), [(18001, 25), (18002, 25), (18003, 25), (18004, 25)])
        self.assertFalse(restored["offers"][-1]["pinned"])
        self.assertEqual(self.edit("cancel")["offers"], published["offers"])

    def test_removing_disabled_offer_preserves_undo_of_temporary_addition(self):
        self.stand.offers.append({"id": 18005, "name": "Fifth"})
        self.remote_offers.append(binding(34, 18004, 9, "disabled"))
        published = stream(self.fetch())
        self.edit("add", 18005)
        self.edit("remove", 18004)
        undone = self.edit("remove", 18005)
        self.assertTrue(undone["has_draft"])
        expected = [row if row["offer_id"] != 18004 else dict(row, removed=True, share=0)
                    for row in published["offers"]]
        self.assertEqual(undone["offers"], expected)

    def test_all_disabled_offers_can_be_removed_and_cancelled(self):
        for row in self.remote_offers:
            row["state"] = "disabled"
        published = stream(self.fetch())
        for row in published["offers"]:
            removed = self.edit("remove", row["offer_id"])
        self.assertTrue(removed["has_draft"])
        self.assertEqual([(row["offer_id"], row["share"], row["removed"]) for row in removed["offers"]],
                         [(18001, 0, True), (18002, 0, True), (18003, 0, True)])
        self.assertEqual(self.edit("cancel")["offers"], published["offers"])

    def test_pin_at_current_share_preserves_weights_without_a_draft(self):
        previous = stream(self.initial)
        pinned = self.edit("pin", 18001)
        self.assertFalse(pinned["has_draft"])
        self.assertGreater(pinned["revision"], previous["revision"])
        self.assertTrue(pinned["offers"][0]["pinned"])
        self.assertEqual(active_shares(pinned), [(18001, 12), (18002, 18), (18003, 70)])
        self.assertEqual(stream(self.snapshot()), pinned)
        stale = self.post_action("remove", 18003, revision=previous["revision"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stream(self.snapshot()), pinned)
        self.assertEqual(self.stand.requests, [])

    def test_pin_with_no_free_offer_for_the_remainder_keeps_snapshot(self):
        self.remote_offers[:] = [binding(31, 18001, 60)]
        before = stream(self.fetch())
        response = self.post_action("pin", 18001)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(stream(self.snapshot()), before)

    def test_pin_unpin_and_disabled_offer(self):
        self.remote_offers[1]["state"] = "disabled"
        self.fetch()
        pinned = self.edit("pin", 18001, share=25)
        self.assertEqual(active_shares(pinned), [(18001, 25), (18003, 75)])
        disabled = next(row for row in pinned["offers"] if row["offer_id"] == 18002)
        self.assertEqual((disabled["state"], disabled["share"], disabled["removed"]), ("disabled", 18, False))
        unpinned = self.edit("unpin", 18001)
        self.assertEqual(active_shares(unpinned), active_shares(pinned))
        self.assertFalse(any(row["pinned"] for row in unpinned["offers"]))
        self.assertEqual(active_shares(self.edit("add", 18004)), [(18001, 33), (18003, 33), (18004, 34)])

    def test_restore_appends_published_binding_unpinned(self):
        self.edit("pin", 18002, share=30)
        removed = self.edit("remove", 18002)
        archived = next(row for row in removed["offers"] if row["offer_id"] == 18002)
        self.assertEqual((archived["removed"], archived["share"]), (True, 0))
        restored = self.edit("restore", 18002)
        self.assertEqual(active_shares(restored), [(18001, 33), (18003, 33), (18002, 34)])
        self.assertFalse(restored["offers"][-1]["pinned"])

    def test_editability_depends_on_schema_and_creator_completion(self):
        self.assertFalse(stream(self.initial, 710)["editable"])
        self.assertIn(self.post_action("add", 18004, stream_id=710).status_code, (409, 422))
        empty = dict(self.stand.streams[41][2], id=713, position=4, name="Empty offers", offers=[])
        self.stand.streams[41].append(empty)
        self.assertTrue(stream(self.fetch(), 713)["editable"])
        draft = self.edit("add", 18004, stream_id=713)
        self.assertEqual(active_shares(draft), [(18004, 100)])
        emptied = self.edit("remove", 18004, stream_id=713)
        self.assertFalse(emptied["has_draft"])
        self.assertEqual(emptied["offers"], [])
        with Session(self.client.app.state.engine) as db, db.begin():
            campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == 41))
            campaign.creation_state = "have_geo"
        self.assertFalse(stream(self.fetch())["editable"])
        self.assertIn(self.post_action("pin", 18001).status_code, (409, 422))

    def test_unknown_offer_and_other_campaign_stream_cannot_change_the_draft(self):
        self.edit("pin", 18002, share=30)
        working = self.snapshot()
        missing_offer = self.post_action("add", 999999)
        self.assertIn(missing_offer.status_code, (404, 422))
        response = self.client.post("/api/campaigns/52/streams/711/draft", json={
            "action": "cancel", "expected_revision": stream(working)["revision"],
        })
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.snapshot(), working)

    def test_stale_edit_cannot_overwrite_newer_draft_or_undo_cancel(self):
        old_revision = stream(self.initial)["revision"]
        draft = self.edit("pin", 18001, share=25)
        stale = self.post_action("remove", 18003, revision=old_revision)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stream(self.snapshot()), draft)
        cancelled = self.edit("cancel")
        stale = self.post_action("add", 18004, revision=draft["revision"])
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stream(self.snapshot()), cancelled)

    def test_refetch_changes_revision_when_published_weights_change(self):
        old = stream(self.initial)["revision"]
        self.remote_offers[1]["share"] = 21
        current = self.fetch()
        self.assertNotEqual(stream(current)["revision"], old)
        rejected = self.post_action("pin", 18001, revision=old)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(self.snapshot(), current)

    def test_fetch_requires_exact_consent_and_retains_drafts_on_upstream_failure(self):
        first = self.edit("remove", 18002)
        second = self.edit("add", 18003, stream_id=712)
        before = self.snapshot()
        versions = {"711": first["revision"], "712": second["revision"]}
        self.stand.requests.clear()
        for data in ({}, {"discard_drafts": {"711": first["revision"]}}):
            response = self.client.post("/api/campaigns/41/fetch", json=data)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.stand.requests, [])
        self.stand.offline = True
        response = self.client.post("/api/campaigns/41/fetch", json={"discard_drafts": versions})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.snapshot(), before)
        self.stand.offline = False
        self.remote_offers[0]["share"] = 17
        fetched = self.fetch(discard_drafts=versions)
        for stream_id in (711, 712):
            self.assertFalse(stream(fetched, stream_id)["has_draft"])
        self.assertEqual(active_shares(stream(fetched)), [(18001, 17), (18002, 18), (18003, 70)])

    def test_concurrent_edit_survives_confirmed_refetch(self):
        first = self.edit("pin", 18001, share=25)
        started, release = Event(), Event()
        upstream = self.client.app.state.keitaro
        read = upstream.campaign_streams

        async def paused(campaign_id):
            rows = await read(campaign_id)
            started.set()
            if not await asyncio.to_thread(release.wait, 5):
                raise AssertionError("Timed out waiting for draft edit")
            return rows

        with patch.object(upstream, "campaign_streams", paused), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.client.post, "/api/campaigns/41/fetch",
                                 json={"discard_drafts": {"711": first["revision"]}})
            try:
                self.assertTrue(started.wait(5))
                newer = self.edit("remove", 18002)
            finally:
                release.set()
            result = future.result(timeout=5)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(stream(self.snapshot()), newer)
