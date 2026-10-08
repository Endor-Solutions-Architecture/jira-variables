"""Pull Endor project and finding identifiers out of a Jira description."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


# Namespace is a single URL segment (it may contain dots). UUIDs are hex.
_PROJECT = re.compile(r"/t/([^/\s\"'\\]+)/projects/([0-9a-fA-F]+)")
_FINDING = re.compile(r"/t/([^/\s\"'\\]+)/findings/([0-9a-fA-F]+)")


@dataclass(frozen=True)
class Identifiers:
    project: tuple[str, str] | None
    findings: tuple[tuple[str, str], ...]


def is_project_aggregation(description: object, issue_type: str | None = None) -> bool:
    """Project aggregation lists every finding on one issue, so no single finding applies.

    Dependency children use a sub-task (or name Package and Dependency in the
    description) and still refer to one dependency's findings.
    """
    text = _as_text(description)
    grouped = (
        "findings are identified" in text
        or "finding is identified" in text
        or "were detected" in text
        or "was detected" in text
    )
    if "Endor-notification-uuid" not in text or not grouped:
        return False
    if "*Dependency*:" in text or "*Package*:" in text:
        return False
    if '"text": "Dependency"' in text or '"text": "Package"' in text:
        return False
    normalized = (issue_type or "").lower().replace(" ", "").replace("_", "")
    if normalized in {"sub-task", "subtask"}:
        return False
    return True


def parse_identifiers(description: object) -> Identifiers:
    """Find the first project link and every finding link, in order."""
    text = _as_text(description)
    project_match = _PROJECT.search(text)
    project = (project_match.group(1), project_match.group(2)) if project_match else None
    seen: set[tuple[str, str]] = set()
    findings: list[tuple[str, str]] = []
    for match in _FINDING.finditer(text):
        item = (match.group(1), match.group(2))
        if item not in seen:
            seen.add(item)
            findings.append(item)
    return Identifiers(project=project, findings=tuple(findings))


def _as_text(description: object) -> str:
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    return json.dumps(description)
