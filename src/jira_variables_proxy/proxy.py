"""Forward Jira REST calls and rewrite issue create and update bodies."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from http.client import HTTPMessage
from urllib.parse import quote, urlsplit

from jira_variables_proxy.config import Config
from jira_variables_proxy.createmeta import page_items, relax_createmeta, restore_custom_value
from jira_variables_proxy.endor import EndorClient
from jira_variables_proxy.errors import ShimError
from jira_variables_proxy.interpolate import InterpolationError, build_context, rewrite_issue


logger = logging.getLogger(__name__)

_CREATE = re.compile(r"^/rest/api/[23]/issue$")
_UPDATE = re.compile(r"^/rest/api/[23]/issue/([^/]+)$")
_API_VERSION = re.compile(r"^/rest/api/([23])/")
_FIELD_META = re.compile(r"^/rest/api/[23]/issue/createmeta/[^/]+/issuetypes/[^/]+$")
_FORWARDED_REQUEST_HEADERS = ("authorization", "content-type", "accept", "x-atlassian-token")
_META_PAGE_SIZE = 50


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class Proxy:
    def __init__(self, config: Config, endor: EndorClient | None = None) -> None:
        self._config = config
        self._endor = endor or EndorClient(
            config.endor_api_url,
            config.endor_api_key,
            config.endor_api_secret,
            config.timeout,
        )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def handle(self, method: str, target: str, headers: HTTPMessage, body: bytes) -> Response:
        parts = urlsplit(target)
        path = parts.path
        if _is_issue_write(method, path):
            body = self._prepare_issue(method, path, headers, body)
        response = self._forward(method, path, parts.query, headers, body)
        if method == "GET" and response.status == 200 and _FIELD_META.match(path):
            return _relax_response(response)
        if _is_issue_write(method, path) and response.status >= 400:
            detail = _jira_failure_detail(response.body)
            if detail:
                logger.info("jira rejected %s %s %s: %s", method, path, response.status, detail)
        return response

    def _prepare_issue(self, method: str, path: str, headers: HTTPMessage, body: bytes) -> bytes:
        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise InterpolationError("issue body is not JSON") from exc
        if not isinstance(payload, dict):
            raise InterpolationError("issue body is not a JSON object")
        if _payload_has_templates(payload):
            fields = payload.get("fields")
            description = fields.get("description") if isinstance(fields, dict) else None
            issue_type = _issue_type_name(fields) if isinstance(fields, dict) else None
            if description is None and method == "PUT":
                description, loaded_type = self._load_upstream_issue(path, headers)
                issue_type = issue_type or loaded_type
            context, finding_present = build_context(description, self._endor, issue_type)
            payload = rewrite_issue(payload, context, finding_present)
        elif not _has_custom_field(payload):
            return body
        return json.dumps(self._restore_custom_fields(path, headers, payload)).encode()

    def _restore_custom_fields(self, path: str, headers: HTTPMessage, payload: dict) -> dict:
        fields = payload.get("fields")
        if not isinstance(fields, dict):
            return payload
        custom = [key for key in fields if key.startswith("customfield_") or key == "priority"]
        if not custom:
            return payload
        project = _project_key(fields)
        issue_type = _issue_type_name(fields)
        if not project or not issue_type:
            return payload
        version = _api_version(path)
        meta = self._load_custom_field_meta(version, project, issue_type, headers)
        for key in custom:
            field = meta.get(key)
            if field is None:
                continue
            name = field.get("name") if isinstance(field.get("name"), str) else key
            try:
                fields[key] = restore_custom_value(fields[key], field, api_version=version)
            except InterpolationError:
                logger.info("rejected custom field %s value %s", name, _log_value(fields[key]))
                raise
            logger.info("restored custom field %s as %s", name, _log_value(fields[key]))
        return payload

    def _load_custom_field_meta(
        self,
        version: str,
        project: str,
        issue_type: str,
        headers: HTTPMessage,
    ) -> dict[str, dict]:
        types_path = f"/rest/api/{version}/issue/createmeta/{quote(project, safe='')}/issuetypes"
        issue_types = self._collect_pages(types_path, headers, "issueTypes")
        match = next((item for item in issue_types if item.get("name") == issue_type), None)
        type_id = match.get("id") if match is not None else None
        if isinstance(type_id, int) and not isinstance(type_id, bool):
            type_id = str(type_id)
        if not isinstance(type_id, str) or not type_id:
            raise InterpolationError(f"issue type {issue_type} was not found for project {project}")
        fields_path = (
            f"/rest/api/{version}/issue/createmeta/{quote(project, safe='')}"
            f"/issuetypes/{quote(type_id, safe='')}"
        )
        fields = self._collect_pages(fields_path, headers, "fields")
        meta = {}
        for field in fields:
            field_id = field.get("fieldId") or field.get("key")
            if isinstance(field_id, str):
                meta[field_id] = field
        return meta

    def _collect_pages(self, path: str, headers: HTTPMessage, kind: str) -> list[dict]:
        start = 0
        items: list[dict] = []
        while True:
            response = self._forward(
                "GET",
                path,
                f"startAt={start}&maxResults={_META_PAGE_SIZE}",
                headers,
                b"",
            )
            if response.status != 200:
                status = response.status if 400 <= response.status < 500 else 502
                raise ShimError(status, "could not load Jira field metadata")
            try:
                payload = json.loads(response.body.decode() or "{}")
            except json.JSONDecodeError as exc:
                raise ShimError(502, "Jira field metadata was not JSON") from exc
            if not isinstance(payload, dict):
                break
            page = page_items(payload, kind)
            if not page:
                break
            items.extend(page)
            total = payload.get("total")
            start += len(page)
            if len(page) < _META_PAGE_SIZE or (isinstance(total, int) and start >= total):
                break
        return items

    def _load_upstream_issue(self, path: str, headers: HTTPMessage) -> tuple[object, str | None]:
        response = self._forward("GET", path, "", headers, b"")
        if response.status != 200:
            raise ShimError(response.status, "could not load the issue description")
        try:
            payload = json.loads(response.body.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise ShimError(502, "upstream issue was not JSON") from exc
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if not isinstance(fields, dict):
            return None, None
        return fields.get("description"), _issue_type_name(fields)

    def _forward(
        self,
        method: str,
        path: str,
        query: str,
        headers: HTTPMessage,
        body: bytes,
    ) -> Response:
        url = self._config.jira_base_url + path
        if query:
            url = url + "?" + query
        request_headers = {}
        for name in _FORWARDED_REQUEST_HEADERS:
            value = headers.get(name)
            if value:
                request_headers[name] = value
        if body:
            request_headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(url, data=body or None, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self._config.timeout) as response:
                return _from_urllib(response.status, response.headers, response.read())
        except urllib.error.HTTPError as exc:
            return _from_urllib(exc.code, exc.headers, exc.read())
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                raise ShimError(504, "jira request timed out") from exc
            logger.warning("upstream jira request failed for %s %s", method, path)
            raise ShimError(502, "jira request failed") from exc


def _issue_type_name(fields: dict) -> str | None:
    issue_type = fields.get("issuetype")
    if not isinstance(issue_type, dict):
        return None
    name = issue_type.get("name")
    return name if isinstance(name, str) else None


def _relax_response(response: Response) -> Response:
    try:
        payload = json.loads(response.body.decode() or "{}")
    except json.JSONDecodeError:
        return response
    if not isinstance(payload, dict):
        return response
    hidden = relax_createmeta(payload)
    if hidden:
        logger.info("hid constraints on %s custom fields", hidden)
    return Response(response.status, response.headers, json.dumps(payload).encode())


def _project_key(fields: dict) -> str | None:
    project = fields.get("project")
    if not isinstance(project, dict):
        return None
    key = project.get("key")
    return key if isinstance(key, str) and key else None


def _api_version(path: str) -> str:
    match = _API_VERSION.match(path)
    return match.group(1) if match else "3"


def _is_issue_write(method: str, path: str) -> bool:
    if method == "POST" and _CREATE.match(path):
        return True
    if method == "PUT" and _UPDATE.match(path):
        return True
    return False


def _has_custom_field(payload: dict) -> bool:
    fields = payload.get("fields")
    return isinstance(fields, dict) and any(key.startswith("customfield_") for key in fields)


def _payload_has_templates(payload: dict) -> bool:
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return False
    for key, value in fields.items():
        if key in {"description", "summary"}:
            continue
        if _contains_template(value):
            return True
    return False


def _contains_template(value: object) -> bool:
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, list):
        return any(_contains_template(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_template(item) for item in value.values())
    return False


def _log_value(value: object) -> str:
    try:
        rendered = json.dumps(value, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > 500:
        return rendered[:500] + "..."
    return rendered


def _jira_failure_detail(body: bytes) -> str | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = " ".join(body.decode(errors="replace").split())
        return text[:500] or None
    if not isinstance(payload, dict):
        return None
    parts: list[str] = []
    messages = payload.get("errorMessages")
    if isinstance(messages, list):
        parts.extend(str(item) for item in messages if item)
    errors = payload.get("errors")
    if isinstance(errors, dict):
        parts.extend(f"{key}: {value}" for key, value in errors.items())
    if not parts:
        return None
    detail = "; ".join(parts)
    return detail[:500]


def _from_urllib(status: int, headers: HTTPMessage, body: bytes) -> Response:
    content_type = headers.get("Content-Type")
    forwarded = [("Content-Type", content_type)] if content_type else []
    return Response(status, forwarded, body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None
