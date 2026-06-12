"""Bearer auth: 401 without token, 200 with, loopback untouched.

Run:  uv run python -m unittest test.web.test_auth -v
"""

from __future__ import annotations

import unittest

import httpx

from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.auth import AuthSettings, check_request
from yuyutsava.daemon.web.services.stream_service import WebHub


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


def _app(host: str, auth: AuthSettings | None):
    return create_app(WebHub(store=object()), host=host, auth=auth)


class CheckRequestTests(unittest.TestCase):
    """The pure decision function, including the /stream query-token rule."""

    settings = AuthSettings(token="sekret", enforce=True)

    def test_bearer_header_accepted(self) -> None:
        self.assertTrue(check_request(
            self.settings, path="/tasks", authorization="Bearer sekret",
        ))

    def test_missing_or_wrong_token_rejected(self) -> None:
        self.assertFalse(check_request(self.settings, path="/tasks"))
        self.assertFalse(check_request(
            self.settings, path="/tasks", authorization="Bearer wrong",
        ))

    def test_query_token_only_on_stream(self) -> None:
        self.assertTrue(check_request(
            self.settings, path="/stream", query_token="sekret",
        ))
        self.assertFalse(check_request(
            self.settings, path="/tasks", query_token="sekret",
        ))

    def test_health_is_public(self) -> None:
        self.assertTrue(check_request(self.settings, path="/health"))

    def test_not_enforced_passes_everything(self) -> None:
        relaxed = AuthSettings(token="", enforce=False)
        self.assertTrue(check_request(relaxed, path="/tasks"))


class AuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_loopback_requires_bearer(self) -> None:
        # /openapi.json: protected (not in the public set) yet stateless, so
        # the assertion exercises auth and nothing else.
        app = _app("100.64.0.1", AuthSettings(token="sekret", enforce=True))
        async with _client(app) as client:
            r = await client.get("/openapi.json")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["code"], "unauthorized")

            r = await client.get(
                "/openapi.json", headers={"Authorization": "Bearer sekret"},
            )
            self.assertEqual(r.status_code, 200)

            # Reachability probe stays open.
            r = await client.get("/health")
            self.assertEqual(r.status_code, 200)

    async def test_loopback_needs_no_token(self) -> None:
        app = _app("127.0.0.1", AuthSettings(token="", enforce=False))
        async with _client(app) as client:
            r = await client.get("/health")
            self.assertEqual(r.status_code, 200)
            r = await client.get("/openapi.json")
            self.assertEqual(r.status_code, 200)

    def test_non_loopback_without_token_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            _app("100.64.0.1", AuthSettings(token="", enforce=False))


if __name__ == "__main__":
    unittest.main()
