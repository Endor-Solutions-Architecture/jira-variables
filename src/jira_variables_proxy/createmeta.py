"""Hide custom-field constraints from Endor, then restore them for Jira.

Endor rejects a select value that is not already an allowed option, and it
rejects a number field that is not an integer. Templates fail both checks.
The field list Endor reads has those constraints removed for custom fields.
Issue create and update put the real shape back after the template is filled.
"""

from __future__ import annotations

import math
import re
from typing import Any

from jira_variables_proxy.interpolate import InterpolationError


_INTEGER = re.compile(r"-?\d+")


def relax_createmeta(payload: dict) -> int:
    """Drop select options and report number custom fields as text.

    The system Priority field is included, because its options are stored as
    name rather than value and Endor then rejects every template. Components
    are left unchanged. Returns how many fields were changed.
    """
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return 0
    changed = 0
    for field in fields:
        if not isinstance(field, dict):
            continue
        if not _is_custom_field(field) and not _is_priority(field):
            continue
        had_options = "allowedValues" in field
        field.pop("allowedValues", None)
        schema = field.get("schema")
        was_number = (
            isinstance(schema, dict)
            and schema.get("type") == "number"
            and _is_custom_field(field)
        )
        if was_number:
            schema["type"] = "string"
        if had_options or was_number:
            changed += 1
    return changed


def restore_custom_value(value: Any, field: dict, *, api_version: str = "3") -> Any:
    """Turn an interpolated custom-field value into the shape Jira stores."""
    if value is None:
        return None
    name = field.get("name") if isinstance(field.get("name"), str) else _field_id(field)
    schema = field.get("schema") if isinstance(field.get("schema"), dict) else {}
    if _is_priority(field):
        return {"name": _match_name(value, _allowed_names(field), name or "Priority")}
    if schema.get("type") == "number":
        return _as_number(value, name or "custom field")

    allowed = _allowed_values(field)
    if not allowed:
        if api_version == "3" and _is_textarea(schema) and isinstance(value, str):
            return _adf_document(value)
        return value
    label = name or "custom field"
    if schema.get("type") == "array":
        items = value if isinstance(value, list) else [value]
        return [{"value": _match(item, allowed, label)} for item in items]
    return {"value": _match(value, allowed, label)}


def page_items(payload: dict, kind: str) -> list[dict] | None:
    """Return one createmeta page, or None when this payload is not that page."""
    keys = ("issueTypes", "issuetypes", "values") if kind == "issueTypes" else (kind,)
    for key in keys:
        page = payload.get(key)
        if isinstance(page, list):
            return [item for item in page if isinstance(item, dict)]
    return None


def _is_textarea(schema: dict) -> bool:
    custom = schema.get("custom")
    return isinstance(custom, str) and custom.endswith(":textarea")


def _adf_document(text: str) -> dict:
    content = []
    for line in text.split("\n"):
        if line:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
        else:
            content.append({"type": "paragraph"})
    if not content:
        content.append({"type": "paragraph"})
    return {"type": "doc", "version": 1, "content": content}


def _is_priority(field: dict) -> bool:
    if _field_id(field) == "priority":
        return True
    schema = field.get("schema")
    if not isinstance(schema, dict):
        return False
    return schema.get("type") == "priority" or schema.get("system") == "priority"


def _allowed_names(field: dict) -> list[str]:
    allowed = field.get("allowedValues")
    if not isinstance(allowed, list):
        return []
    names = []
    for item in allowed:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _match_name(value: Any, allowed: list[str], field_name: str) -> str:
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        value = value["name"]
    if isinstance(value, str) and value in allowed:
        return value
    choices = ", ".join(allowed)
    raise InterpolationError(f"{field_name} must be one of {choices}")


def _is_custom_field(field: dict) -> bool:
    field_id = _field_id(field)
    return field_id.startswith("customfield_")


def _field_id(field: dict) -> str:
    for key in ("fieldId", "key"):
        value = field.get(key)
        if isinstance(value, str):
            return value
    return ""


def _allowed_values(field: dict) -> list[str]:
    allowed = field.get("allowedValues")
    if not isinstance(allowed, list):
        return []
    values = []
    for item in allowed:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            values.append(item["value"])
    return values


def _match(value: Any, allowed: list[str], field_name: str) -> str:
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        value = value["value"]
    if isinstance(value, str) and value in allowed:
        return value
    choices = ", ".join(allowed)
    raise InterpolationError(f"{field_name} must be one of {choices}")


def _as_number(value: Any, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InterpolationError(f"{field_name} is not a number")
    if isinstance(value, str):
        if _INTEGER.fullmatch(value):
            return int(value)
        try:
            value = float(value)
        except ValueError as exc:
            raise InterpolationError(f"{field_name} is not a number") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise InterpolationError(f"{field_name} is not a number")
    return value
