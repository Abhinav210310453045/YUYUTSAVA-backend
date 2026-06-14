"""Bearer auth: 401 without token, 200 with, loopback untouched.

Run:  uv run python -m unittest test.web.test_auth -v
"""

from __future__ import annotations

import unittest

import httpx

from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.auth import AuthSettings, check_request
from yuyutsava.daemon.web.services.stream_service import WebHub


def _client(app, *, peer: str = "127.0.0.1") -> httpx.AsyncClient:
    # ``client`` sets request.client.host so tests can simulate a loopback
    # renderer vs an off-box (tailnet) peer.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
        base_url="http://test",
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

    def test_loopback_peer_exempt_when_enforced(self) -> None:
        # 0.0.0.0 bind enforces auth, but loopback peers (the Electron
        # renderer) are exempt without a token.
        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(check_request(
                self.settings, path="/tasks", peer_host=peer,
            ), peer)

    def test_non_loopback_peer_still_needs_token(self) -> None:
        self.assertFalse(check_request(
            self.settings, path="/tasks", peer_host="100.64.0.1",
        ))
        self.assertTrue(check_request(
            self.settings, path="/tasks", peer_host="100.64.0.1",
            authorization="Bearer sekret",
        ))


class AuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_loopback_requires_bearer(self) -> None:
        # /openapi.json: protected (not in the public set) yet stateless, so
        # the assertion exercises auth and nothing else.
        app = _app("100.64.0.1", AuthSettings(token="sekret", enforce=True))
        # Off-box peer: the loopback exemption does not apply.
        async with _client(app, peer="100.64.0.2") as client:
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

    async def test_loopback_peer_exempt_on_non_loopback_bind(self) -> None:
        # 0.0.0.0-style bind enforces auth, but the local renderer (loopback
        # peer) reaches protected routes without a token.
        app = _app("0.0.0.0", AuthSettings(token="sekret", enforce=True))
        async with _client(app, peer="127.0.0.1") as client:
            r = await client.get("/openapi.json")
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
