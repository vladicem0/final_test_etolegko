import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app


class CatalogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "DATABASE_URL": f"sqlite:///{Path(directory).as_posix()}/test.sqlite3",
            "KEITARO_BASE_URL": "https://stand.example/admin_api/v1",
            "KEITARO_API_KEY": "test-fixture",
        }))
        command.upgrade(Config("alembic.ini"), "head")
        self.requests = []
        self.reply = httpx.Response(200, json=[])
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.upstream)
        self.enterContext(patch(
            "app.main.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        ))
        self.client = self.enterContext(TestClient(app))

    def upstream(self, request):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def test_catalog_routes_and_auth(self):
        self.reply = httpx.Response(200, json=[{"id": 12, "name": "Example", "extra": "omit"}])
        routes = {
            "/api/domains": "domains",
            "/api/groups": "groups?type=campaigns",
            "/api/traffic-sources": "traffic_sources",
            "/api/offers": "offers",
        }
        for route, upstream in routes.items():
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [{"id": 12, "name": "Example"}])
                request = self.requests[-1]
                self.assertEqual(str(request.url), "https://stand.example/admin_api/v1/" + upstream)
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.headers["Api-Key"], "test-fixture")

    def test_offer_search_uses_exact_id_or_name_substring(self):
        items = [{"id": 12, "name": "First"}, {"id": 123, "name": "Тестовый оффер"}]
        for term, expected in [("12", items[:1]), (" ОФФЕР ", items[1:]), ("missing", []), ("", items)]:
            with self.subTest(term=term):
                self.reply = httpx.Response(200, json=items)
                response = self.client.get("/api/offers", params={"q": term})
                self.assertEqual(response.json(), expected)
                self.assertFalse(self.requests[-1].url.query)

    def test_upstream_errors(self):
        for reply, status in [
            (httpx.Response(401, text="upstream-private-detail"), 502),
            (httpx.Response(200, text="upstream-private-detail"), 502),
            (httpx.Response(200, json=[{"id": 3}]), 502),
            (httpx.ReadTimeout("upstream-private-detail"), 504),
            (httpx.ConnectError("upstream-private-detail"), 502),
        ]:
            with self.subTest(reply=reply):
                self.reply = reply
                response = self.client.get("/api/domains")
                self.assertEqual(response.status_code, status)
                self.assertNotIn("upstream-private-detail", response.text)
                self.assertNotIn("test-fixture", response.text)
