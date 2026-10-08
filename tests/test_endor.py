"""Startup credential check against a stand-in Endor auth endpoint."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jira_variables_proxy.endor import EndorClient
from jira_variables_proxy.errors import ShimError


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.bodies.append(body)  # type: ignore[attr-defined]
        status, payload = self.server.respond(body)  # type: ignore[attr-defined]
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        return


class CredentialCheckTest(unittest.TestCase):
    def _serve(self, respond) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.respond = respond  # type: ignore[attr-defined]
        server.bodies = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def test_accepted_credentials_are_exchanged_once(self) -> None:
        def respond(body: dict) -> tuple[int, dict]:
            self.assertEqual(body["key"], "key")
            self.assertEqual(body["secret"], "secret")
            return 200, {"token": "bearer", "expiration_time": "2099-01-01T00:00:00Z"}

        server = self._serve(respond)
        host, port = server.server_address[:2]
        client = EndorClient(f"http://{host}:{port}", "key", "secret", 5)
        client.check_credentials()
        client.check_credentials()
        self.assertEqual(len(server.bodies), 1)  # type: ignore[attr-defined]

    def test_rejected_credentials_fail(self) -> None:
        server = self._serve(lambda _body: (401, {"message": "unauthenticated"}))
        host, port = server.server_address[:2]
        client = EndorClient(f"http://{host}:{port}", "key", "bad", 5)
        with self.assertRaises(ShimError) as caught:
            client.check_credentials()
        self.assertIn("401", caught.exception.message)
        self.assertNotIn("bad", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
