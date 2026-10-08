"""Expand {{project…}}, {{finding…}}, {{packageversion…}}, and {{repositoryversion…}} paths."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from jira_variables_proxy.description import is_project_aggregation, parse_identifiers
from jira_variables_proxy.endor import EndorClient, choose_finding
from jira_variables_proxy.errors import ShimError


_TEMPLATE = re.compile(
    r"\{\{\s*((?:project|finding|packageversion|repositoryversion)(?:\.[A-Za-z0-9_-]+)+)"
    r"(?:\s*\|\s*([A-Za-z_][A-Za-z0-9_]*))?\s*\}\}"
)
_ROOTS = frozenset({"project", "finding", "packageversion", "repositoryversion"})
_FINDING_ROOTS = frozenset({"finding", "packageversion", "repositoryversion"})
# Longest first so FINDING_LEVEL_ wins over LEVEL_.
_ENUM_PREFIXES = tuple(
    sorted(
        (
            "CONTEXT_TYPE_",
            "CVSS_VERSION_",
            "ECOSYSTEM_",
            "FINDING_CATEGORY_",
            "FINDING_LEVEL_",
            "FINDING_REMEDIATION_",
            "FINDING_TAGS_",
            "LEVEL_",
            "NVD_SEVERITY_",
            "PLATFORM_SOURCE_",
            "SCAN_STATE_",
            "SYSTEM_EVALUATION_METHOD_",
            "VALIDATION_STATUS_",
        ),
        key=len,
        reverse=True,
    )
)
_UNCHANGED_FIELDS = frozenset({"description", "summary"})
_SCALAR = (str, int, float, bool)

logger = logging.getLogger(__name__)


class InterpolationError(ShimError):
    def __init__(self, message: str) -> None:
        super().__init__(400, message)


def build_context(
    description: object,
    endor: EndorClient,
    issue_type: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Load the project and, when one finding applies, that finding.

    Project aggregation lists every finding on the issue. Finding templates
    are left off that issue. A dependency child still resolves its findings.
    When that finding's parent is a PackageVersion or a RepositoryVersion, that
    object is loaded from meta.parent_uuid. A finding has one parent, so only
    one of those two is loaded.
    """
    identifiers = parse_identifiers(description)
    context: dict[str, Any] = {}
    finding_present = False
    skip_finding = bool(identifiers.findings) and is_project_aggregation(description, issue_type)

    if identifiers.findings and not skip_finding:
        findings = [endor.get_finding(namespace, uuid) for namespace, uuid in identifiers.findings]
        context["finding"] = choose_finding(findings)
        finding_present = True
        parent = _finding_parent(context["finding"], endor)
        if parent is not None:
            root, document = parent
            context[root] = document

    if identifiers.project is not None:
        namespace, uuid = identifiers.project
        context["project"] = endor.get_project(namespace, uuid)
    elif finding_present:
        finding = context["finding"]
        spec = finding.get("spec") if isinstance(finding.get("spec"), dict) else {}
        tenant = finding.get("tenant_meta") if isinstance(finding.get("tenant_meta"), dict) else {}
        project_uuid = spec.get("project_uuid")
        namespace = tenant.get("namespace")
        if not isinstance(project_uuid, str) or not project_uuid or not isinstance(namespace, str):
            raise InterpolationError("finding did not identify a project")
        context["project"] = endor.get_project(namespace, project_uuid)
    else:
        raise InterpolationError("description has no project or finding link")

    return context, finding_present


def rewrite_issue(payload: dict, context: dict[str, Any], finding_present: bool) -> dict:
    """Expand templates Endor placed in labels and custom field values.

    The description is left unchanged so Endor-notification-uuid stays searchable.
    A template that cannot be resolved becomes null. A placeholder whose object
    was not loaded is left out, so a value can name both a package version and
    a repository version and keep the one that applies. An unresolved label is
    left off the label list, because a label cannot be null.
    """
    issue = copy.deepcopy(payload)
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        raise InterpolationError("issue payload has no fields object")

    for key, value in list(fields.items()):
        if key in _UNCHANGED_FIELDS:
            continue
        fields[key] = _expand_node(value, context, finding_present, label=key == "labels")
    return issue


def resolve_path(path: str, context: dict[str, Any], *, label: bool = False) -> str:
    """Resolve one dotted path such as project.meta.name.

    label strips a known Endor enum prefix and title-cases the remainder,
    so FINDING_LEVEL_MEDIUM becomes Medium.
    """
    segments = path.split(".")
    if "ingestion_token" in segments:
        raise InterpolationError("refusing to read a credential field")
    if not segments or segments[0] not in _ROOTS:
        raise InterpolationError(f"unsupported template path {path}")
    if segments[0] not in context:
        raise InterpolationError(f"no {segments[0]} is available for {path}")
    try:
        value = _walk(context[segments[0]], segments[1:], path)
    except InterpolationError:
        raise
    except Exception as exc:  # pragma: no cover - walk raises InterpolationError
        raise InterpolationError(f"could not resolve {path}") from exc
    rendered = _stringify(value, path)
    if label:
        rendered = _label(rendered, path)
    return rendered


def _expand_node(value: Any, context: dict[str, Any], finding_present: bool, *, label: bool) -> Any:
    if isinstance(value, str):
        if not _TEMPLATE.search(value):
            return value
        if not finding_present and _needs_finding(value):
            logger.info("left %s unset because this issue has no single finding", value)
            return None
        rendered = _expand_text(value, context, finding_present)
        if rendered is None:
            return None
        if label and " " in rendered:
            raise InterpolationError("resolved label contains a space")
        return rendered
    if isinstance(value, list):
        expanded = []
        for item in value:
            rendered = _expand_node(item, context, finding_present, label=label)
            if rendered is None or rendered == "":
                continue
            expanded.append(rendered)
        return expanded
    if isinstance(value, dict):
        return {
            key: _expand_node(item, context, finding_present, label=False)
            for key, item in value.items()
        }
    return value


def _expand_text(text: str, context: dict[str, Any], finding_present: bool) -> str | None:
    unresolved = False
    resolved = False

    def replace(match: re.Match[str]) -> str:
        nonlocal unresolved, resolved
        path = match.group(1)
        filter_name = match.group(2)
        if filter_name not in {None, "label"}:
            raise InterpolationError(f"unsupported template filter {filter_name}")
        if not finding_present and _rooted_in_finding(path):
            return ""
        root = path.split(".", 1)[0]
        if root not in context:
            logger.info("left {{%s}} unset because no %s is available", path, root)
            return ""
        try:
            value = resolve_path(path, context, label=filter_name == "label")
        except InterpolationError as exc:
            if "credential" in exc.message:
                raise
            logger.info("could not resolve {{%s}}: %s", path, exc.message)
            unresolved = True
            return ""
        resolved = True
        return value

    rendered = _TEMPLATE.sub(replace, text)
    if unresolved or not resolved or rendered == "":
        return None
    return rendered


def _needs_finding(text: str) -> bool:
    return any(_rooted_in_finding(match.group(1)) for match in _TEMPLATE.finditer(text))


def _rooted_in_finding(path: str) -> bool:
    return path.split(".", 1)[0] in _FINDING_ROOTS


def _finding_parent(finding: dict, endor: EndorClient) -> tuple[str, dict] | None:
    meta = finding.get("meta") if isinstance(finding.get("meta"), dict) else {}
    kind = meta.get("parent_kind")
    if kind == "PackageVersion":
        root, fetch = "packageversion", endor.get_package_version
    elif kind == "RepositoryVersion":
        root, fetch = "repositoryversion", endor.get_repository_version
    else:
        return None
    uuid = meta.get("parent_uuid")
    tenant = finding.get("tenant_meta") if isinstance(finding.get("tenant_meta"), dict) else {}
    namespace = tenant.get("namespace")
    if not isinstance(uuid, str) or not uuid or not isinstance(namespace, str) or not namespace:
        return None
    return root, fetch(namespace, uuid)


def _walk(node: Any, segments: list[str], path: str) -> Any:
    if not segments:
        return node
    if node is None:
        raise InterpolationError(f"{path} is empty")
    if isinstance(node, dict):
        for length in range(len(segments), 0, -1):
            key = ".".join(segments[:length])
            if key in node:
                return _walk(node[key], segments[length:], path)
        raise InterpolationError(f"field {'.'.join(segments)} is absent")
    if isinstance(node, list):
        if _name_value_tags(node):
            return _walk(_tag_value(node, segments[0]), segments[1:], path)
        raise InterpolationError(f"{path} continues through a repeated field")
    raise InterpolationError(f"{path} continues past a value")


def _name_value_tags(items: list) -> bool:
    return bool(items) and all(isinstance(item, str) for item in items)


def _tag_value(items: list, name: str) -> str:
    for item in items:
        key, separator, value = item.partition("=")
        if separator and key == name:
            return value
    raise InterpolationError(f"field {name} is absent")


def _label(value: str, path: str) -> str:
    for prefix in _ENUM_PREFIXES:
        if value.startswith(prefix) and len(value) > len(prefix):
            words = value[len(prefix) :].split("_")
            if all(words):
                return " ".join(word.capitalize() for word in words)
    raise InterpolationError(f"{path} has no label")


def _stringify(value: Any, path: str) -> str:
    if value is None or value == "":
        raise InterpolationError(f"{path} is empty")
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            raise InterpolationError(f"{path} is empty")
        if not all(isinstance(item, _SCALAR) and item != "" for item in value):
            raise InterpolationError(f"{path} is a repeated message")
        return ",".join(_stringify(item, path) for item in value)
    raise InterpolationError(f"{path} is not a value")
