"""Endor API client used to load Project, Finding, PackageVersion, and RepositoryVersion documents."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

from jira_variables_proxy.errors import ShimError


_SEVERITY_RANK = {
    "FINDING_LEVEL_CRITICAL": 0,
    "FINDING_LEVEL_HIGH": 1,
    "FINDING_LEVEL_MEDIUM": 2,
    "FINDING_LEVEL_LOW": 3,
}


class EndorClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = timeout
        self._token = ""
        self._token_expiry = 0.0

    def check_credentials(self) -> None:
        """Exchange the API key for a token.

        A namespace is not required. This proves the key and secret are
        accepted. It does not prove the key can read every namespace that
        will raise a ticket.
        """
        self._bearer()

    def get_project(self, namespace: str, uuid: str) -> dict:
        return self._get_object("projects", namespace, uuid)

    def get_finding(self, namespace: str, uuid: str) -> dict:
        return self._get_object("findings", namespace, uuid)

    def get_package_version(self, namespace: str, uuid: str) -> dict:
        return self._get_object("package-versions", namespace, uuid)

    def get_repository_version(self, namespace: str, uuid: str) -> dict:
        return self._get_object("repository-versions", namespace, uuid)

    def _get_object(self, kind: str, namespace: str, uuid: str) -> dict:
        path = f"/v1/namespaces/{quote(namespace, safe='')}/{kind}/{quote(uuid, safe='')}"
        payload = self._request("GET", path, None)
        if not isinstance(payload, dict):
            raise ShimError(502, f"endor {kind} response was not an object")
        return payload

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if path != "/v1/auth/api-key":
            headers["Authorization"] = "Bearer " + self._bearer()
        request = urllib.request.Request(
            self._base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ShimError(502, f"endor {method} {path} returned {exc.code}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if _is_timeout(exc):
                raise ShimError(504, "endor request timed out") from exc
            raise ShimError(502, "endor request failed") from exc
        try:
            parsed = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise ShimError(502, "endor response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise ShimError(502, "endor response was not an object")
        return parsed

    def _bearer(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        payload = self._request(
            "POST",
            "/v1/auth/api-key",
            {"key": self._api_key, "secret": self._api_secret},
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ShimError(502, "endor authentication response had no token")
        self._token = token
        self._token_expiry = _expiry(payload.get("expiration_time"))
        return token


def choose_finding(findings: list[dict]) -> dict:
    """Pick the highest severity finding. Ties keep the earlier description order."""
    best = findings[0]
    best_rank = _rank(best)
    for finding in findings[1:]:
        rank = _rank(finding)
        if rank < best_rank:
            best = finding
            best_rank = rank
    return best


def _rank(finding: dict) -> int:
    spec = finding.get("spec")
    level = spec.get("level") if isinstance(spec, dict) else None
    if not isinstance(level, str):
        return 4
    return _SEVERITY_RANK.get(level, 4)


def _expiry(value: object) -> float:
    if isinstance(value, str) and value:
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.timestamp() - 60
    return time.time() + 600


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, TimeoutError)
