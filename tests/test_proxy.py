"""Proxy behavior against stand-in Jira and Endor servers."""

from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jira_variables_proxy.config import Config
from jira_variables_proxy.server import make_server


_FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINDING = json.loads((_FIXTURES / "finding.json").read_text())
PROJECT = json.loads((_FIXTURES / "project.json").read_text())

_PROJECT_LINK = "https://app.endorlabs.com/t/david-learn/projects/69f5b8c23f06b1175a2023c0"
_FINDING_LINK = "https://app.endorlabs.com/t/david-learn/findings/6a44ee0cfc31b281d73bb9f5"
_DESCRIPTION = (
    f"*Project*: [saleor|{_PROJECT_LINK}]\n"
    f"*More details*: [Finding: 6a44ee0cfc31b281d73bb9f5|{_FINDING_LINK}]\n"
    "*Endor-notification-uuid*: 6a44ee18e17f6edd32c349bc"
)


class _Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._record()

    def do_POST(self) -> None:  # noqa: N802
        self._record()

    def do_PUT(self) -> None:  # noqa: N802
        self._record()

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        status, payload = self.server.respond(  # type: ignore[attr-defined]
            self.command,
            self.path,
            body,
            self.headers.get("Authorization"),
        )
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        return


class _FakeServer(ThreadingHTTPServer):
    def __init__(self, respond) -> None:
        super().__init__(("127.0.0.1", 0), _Recorder)
        self.requests: list[dict] = []
        self.respond = respond


def _serve(server: ThreadingHTTPServer) -> None:
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()


def _url(server: ThreadingHTTPServer, path: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def _basic(username: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()


def _request(url: str, method: str = "GET", body: bytes | None = None, auth: str | None = None):
    headers = {}
    if auth:
        headers["Authorization"] = auth
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class ProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.jira = _FakeServer(self._jira_response)
        self.endor = _FakeServer(self._endor_response)
        _serve(self.jira)
        _serve(self.endor)
        self.config = Config(
            listen_host="127.0.0.1",
            listen_port=0,
            public_base_url="http://shim.example",
            jira_base_url=_url(self.jira, ""),
            endor_api_url=_url(self.endor, ""),
            endor_api_key="key",
            endor_api_secret="secret",
        )
        self.shim = make_server(self.config)
        _serve(self.shim)
        self.auth = _basic("endor-user", "endor-token")
        self.issue_error = None

    def tearDown(self) -> None:
        for server in (self.shim, self.jira, self.endor):
            server.shutdown()
            server.server_close()

    def test_tenant_info_is_local_and_has_an_empty_cloud_id(self) -> None:
        status, body = _request(_url(self.shim, "/_edge/tenant_info"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"cloudId": "", "baseUrl": "http://shim.example"})
        self.assertEqual(self.jira.requests, [])

    def test_browse_redirects_to_the_configured_jira_site(self) -> None:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(_url(self.shim, "/browse/SCRUM-5"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(
            caught.exception.headers.get("Location"),
            _url(self.jira, "/browse/SCRUM-5"),
        )
        self.assertEqual(self.jira.requests, [])

    def test_caller_authorization_is_forwarded_unchanged(self) -> None:
        status, body = _request(
            _url(self.shim, "/rest/api/3/issue/createmeta/JID/issuetypes"),
            auth=_basic("endor-user", "nope"),
        )
        self.assertEqual(status, 401)
        self.assertIn("authentication failed", json.loads(body)["errorMessages"][0])
        self.assertEqual(self.jira.requests[0]["authorization"], _basic("endor-user", "nope"))

    def test_createmeta_is_forwarded_unchanged(self) -> None:
        path = "/rest/api/3/issue/createmeta/JID/issuetypes?startAt=0&maxResults=50"
        status, body = _request(_url(self.shim, path), auth=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"values": [{"name": "Task"}]})
        recorded = self.jira.requests[0]
        self.assertEqual(recorded["method"], "GET")
        self.assertEqual(recorded["path"], path)
        self.assertEqual(recorded["authorization"], self.auth)

    def test_custom_field_createmeta_hides_options_and_number_type(self) -> None:
        status, body = _request(
            _url(self.shim, "/rest/api/3/issue/createmeta/JID/issuetypes/10001"),
            auth=self.auth,
        )
        self.assertEqual(status, 200)
        fields = {field["fieldId"]: field for field in json.loads(body)["fields"]}
        self.assertNotIn("allowedValues", fields["customfield_10020"])
        self.assertEqual(fields["customfield_10021"]["schema"]["type"], "string")
        self.assertNotIn("allowedValues", fields["priority"])
        self.assertIn("allowedValues", self._field_meta()["fields"][0])

    def test_create_restores_select_and_number_custom_fields(self) -> None:
        payload = {
            "fields": {
                "project": {"key": "SCRUM"},
                "issuetype": {"name": "Task"},
                "summary": "Unmaintained Dependency",
                "description": _DESCRIPTION,
                "customfield_10020": "{{finding.spec.level | label}}",
                "customfield_10021": "5.3",
                "customfield_10022": "https://github.com/TreetopTechie/saleor.git",
            }
        }
        status, body = _request(
            _url(self.shim, "/rest/api/3/issue"),
            method="POST",
            body=json.dumps(payload).encode(),
            auth=self.auth,
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["key"], "JID-1576")
        fields = json.loads(self._jira_call("POST", "/rest/api/3/issue")["body"])["fields"]
        self.assertEqual(fields["customfield_10020"], {"value": "Medium"})
        self.assertEqual(fields["customfield_10021"], 5.3)
        self.assertEqual(fields["customfield_10022"], "https://github.com/TreetopTechie/saleor.git")

    def test_create_rejects_a_select_value_jira_does_not_allow(self) -> None:
        payload = {
            "fields": {
                "project": {"key": "SCRUM"},
                "issuetype": {"name": "Task"},
                "summary": "Unmaintained Dependency",
                "customfield_10020": "Urgent",
            }
        }
        status, body = _request(
            _url(self.shim, "/rest/api/3/issue"),
            method="POST",
            body=json.dumps(payload).encode(),
            auth=self.auth,
        )
        self.assertEqual(status, 400)
        self.assertIn("Severity must be one of", json.loads(body)["errorMessages"][0])
        self.assertFalse(any(call["method"] == "POST" for call in self.jira.requests))

    def test_create_logs_the_value_a_select_rejects(self) -> None:
        payload = {
            "fields": {
                "project": {"key": "SCRUM"},
                "issuetype": {"name": "Task"},
                "summary": "Unmaintained Dependency",
                "customfield_10020": "Urgent",
            }
        }
        with self.assertLogs("jira_variables_proxy.proxy", level="INFO") as logs:
            status, _body = _request(
                _url(self.shim, "/rest/api/3/issue"),
                method="POST",
                body=json.dumps(payload).encode(),
                auth=self.auth,
            )
        self.assertEqual(status, 400)
        self.assertIn('rejected custom field Severity value "Urgent"', "\n".join(logs.output))

    def test_create_logs_the_restored_value_and_jira_rejection(self) -> None:
        self.issue_error = {
            "errorMessages": ["Issue create failed"],
            "errors": {"customfield_10020": "Option value 'Medium' is not valid"},
        }
        payload = {
            "fields": {
                "project": {"key": "SCRUM"},
                "issuetype": {"name": "Task"},
                "summary": "Unmaintained Dependency",
                "description": _DESCRIPTION,
                "customfield_10020": "{{finding.spec.level | label}}",
            }
        }
        with self.assertLogs("jira_variables_proxy.proxy", level="INFO") as logs:
            status, body = _request(
                _url(self.shim, "/rest/api/3/issue"),
                method="POST",
                body=json.dumps(payload).encode(),
                auth=self.auth,
            )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["errors"]["customfield_10020"], "Option value 'Medium' is not valid")
        text = "\n".join(logs.output)
        self.assertIn('restored custom field Severity as {"value": "Medium"}', text)
        self.assertIn(
            "jira rejected POST /rest/api/3/issue 400: "
            "Issue create failed; customfield_10020: Option value 'Medium' is not valid",
            text,
        )

    def test_create_rewrites_labels_and_bindings(self) -> None:
        payload = {
            "fields": {
                "summary": "Unmaintained Dependency",
                "description": _DESCRIPTION,
                "labels": ["endorlabs-scan", "{{project.spec.git.http_clone_url}}"],
                "customfield_10001": "{{project.spec.internal_reference_key}}",
                "customfield_10002": "{{finding.spec.level}}",
                "priority": {"name": "{{finding.spec.level}}"},
            }
        }
        status, body = _request(
            _url(self.shim, "/rest/api/3/issue"),
            method="POST",
            body=json.dumps(payload).encode(),
            auth=self.auth,
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["key"], "JID-1576")
        forwarded = json.loads(self._jira_call("POST", "/rest/api/3/issue")["body"])
        fields = forwarded["fields"]
        self.assertEqual(fields["description"], _DESCRIPTION)
        self.assertEqual(
            fields["labels"],
            ["endorlabs-scan", "https://github.com/TreetopTechie/saleor.git"],
        )
        self.assertEqual(fields["customfield_10001"], "https://github.com/TreetopTechie/saleor.git")
        self.assertEqual(fields["customfield_10002"], "FINDING_LEVEL_MEDIUM")
        self.assertEqual(fields["priority"], {"name": "FINDING_LEVEL_MEDIUM"})
        self.assertEqual(self.endor.requests[0]["path"], "/v1/auth/api-key")
        self.assertNotIn(b"secret", self.jira.requests[-1]["body"])

    def test_label_only_update_loads_the_description_then_rewrites(self) -> None:
        payload = {
            "fields": {
                "labels": ["endorlabs-scan", "{{finding.spec.level}}"],
                "priority": {"name": "{{finding.spec.level}}"},
            }
        }
        status, _body = _request(
            _url(self.shim, "/rest/api/3/issue/JID-1576"),
            method="PUT",
            body=json.dumps(payload).encode(),
            auth=self.auth,
        )
        self.assertEqual(status, 204)
        methods = [call["method"] for call in self.jira.requests]
        self.assertEqual(methods, ["GET", "PUT"])
        self.assertEqual(
            [call["authorization"] for call in self.jira.requests],
            [self.auth, self.auth],
        )
        forwarded = json.loads(self.jira.requests[1]["body"])
        self.assertEqual(
            forwarded["fields"]["labels"],
            ["endorlabs-scan", "FINDING_LEVEL_MEDIUM"],
        )
        self.assertEqual(forwarded["fields"]["priority"], {"name": "FINDING_LEVEL_MEDIUM"})

    def _field_meta(self) -> dict:
        return {
            "total": 4,
            "fields": [
                {
                    "name": "Severity",
                    "fieldId": "customfield_10020",
                    "schema": {"type": "option"},
                    "allowedValues": [
                        {"value": "Critical"},
                        {"value": "High"},
                        {"value": "Medium"},
                        {"value": "Low"},
                    ],
                },
                {
                    "name": "CVSS 3.1",
                    "fieldId": "customfield_10021",
                    "schema": {"type": "number"},
                },
                {
                    "name": "References",
                    "fieldId": "customfield_10022",
                    "schema": {"type": "string"},
                },
                {
                    "name": "Priority",
                    "fieldId": "priority",
                    "schema": {"type": "priority"},
                    "allowedValues": [{"name": "Medium", "id": "3"}],
                },
            ],
        }

    def _jira_call(self, method: str, path: str) -> dict:
        for call in self.jira.requests:
            if call["method"] == method and call["path"] == path:
                return call
        self.fail(f"{method} {path} was not forwarded")
        return {}

    def _jira_response(self, method: str, path: str, body: bytes, authorization: str | None):
        if authorization != self.auth:
            return 401, {"errorMessages": ["authentication failed"]}
        bare = path.split("?", 1)[0]
        if method == "GET" and bare.endswith("/issuetypes/10001"):
            return 200, self._field_meta()
        if method == "GET" and bare.endswith("/createmeta/SCRUM/issuetypes"):
            return 200, {"issueTypes": [{"id": "10001", "name": "Task"}], "total": 1}
        if method == "GET" and path.startswith("/rest/api/3/issue/createmeta/"):
            return 200, {"values": [{"name": "Task"}]}
        if method == "GET" and path == "/rest/api/3/issue/JID-1576":
            return 200, {"key": "JID-1576", "fields": {"description": _DESCRIPTION}}
        if method == "POST" and path == "/rest/api/3/issue":
            if self.issue_error is not None:
                return 400, self.issue_error
            return 201, {"id": "1", "key": "JID-1576"}
        if method == "PUT" and path == "/rest/api/3/issue/JID-1576":
            return 204, {}
        return 404, {"errorMessages": [f"unexpected {method} {path}"]}

    def _endor_response(self, method: str, path: str, body: bytes, authorization: str | None):
        if method == "POST" and path == "/v1/auth/api-key":
            sent = json.loads(body)
            self.assertEqual(sent["key"], "key")
            self.assertEqual(sent["secret"], "secret")
            return 200, {"token": "endor-bearer", "expiration_time": "2099-01-01T00:00:00Z"}
        if path.endswith("/projects/69f5b8c23f06b1175a2023c0"):
            return 200, PROJECT
        if path.endswith("/findings/6a44ee0cfc31b281d73bb9f5"):
            return 200, FINDING
        if path.endswith("/package-versions/6a44edb5006c73f62271179f"):
            return 200, {"uuid": "6a44edb5006c73f62271179f", "meta": {"name": "pypi://saleor@3.22.0-a.0"}}
        return 404, {"error": f"unexpected {method} {path}"}


if __name__ == "__main__":
    unittest.main()
