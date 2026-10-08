"""HTTP server Endor points at as its Jira base URL."""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jira_variables_proxy.config import Config
from jira_variables_proxy.errors import ShimError, jira_error_body
from jira_variables_proxy.proxy import Proxy


logger = logging.getLogger(__name__)

_BROWSE = re.compile(r"^/browse/([A-Z][A-Z0-9_]*-\d+)$")


def make_server(config: Config, proxy: Proxy | None = None) -> ThreadingHTTPServer:
    handler = _handler_factory(config, proxy or Proxy(config))
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
    server.timeout = config.timeout
    return server


def _handler_factory(config: Config, proxy: Proxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if self.command == "GET" and path == "/healthz":
                    self._send(200, [("Content-Type", "application/json")], b'{"status":"ok"}')
                    return
                if self.command == "GET" and path == "/_edge/tenant_info":
                    body = json.dumps(
                        {"cloudId": "", "baseUrl": config.public_base_url}
                    ).encode()
                    self._send(200, [("Content-Type", "application/json")], body)
                    return
                browse = _BROWSE.match(path) if self.command == "GET" else None
                if browse:
                    location = f"{config.jira_base_url}/browse/{browse.group(1)}"
                    self._send(302, [("Location", location)], b"")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length else b""
                response = proxy.handle(self.command, self.path, self.headers, body)
                self._send(response.status, response.headers, response.body)
            except ShimError as exc:
                self._error = exc.message
                self._send(exc.status, [("Content-Type", "application/json")], jira_error_body(exc.message))
            except Exception:
                logger.exception("request failed")
                self._send(500, [("Content-Type", "application/json")], jira_error_body("internal error"))

        def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return
            code = args[1] if len(args) > 1 else "-"
            detail = getattr(self, "_error", None)
            if detail:
                logger.info("%s %s %s %s", self.command, path, code, detail)
            else:
                logger.info("%s %s %s", self.command, path, code)

    return Handler
