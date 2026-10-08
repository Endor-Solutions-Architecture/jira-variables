"""Runtime configuration loaded from a .env file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    public_base_url: str
    jira_base_url: str
    endor_api_url: str
    endor_api_key: str
    endor_api_secret: str
    timeout: float = 35.0

    @classmethod
    def from_dotenv(cls, path: str | Path | None = None) -> Config:
        file_path = Path.cwd() / ".env" if path is None else Path(path)
        if not file_path.is_file():
            raise ValueError(f".env file is required ({file_path})")
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"could not read {file_path}") from exc
        return cls.from_mapping(parse_dotenv(text))

    @classmethod
    def from_mapping(cls, source: dict[str, str]) -> Config:
        host, port = _parse_listen(source.get("LISTEN_ADDR", ":8080").strip() or ":8080")
        return cls(
            listen_host=host,
            listen_port=port,
            public_base_url=_required(source, "PUBLIC_BASE_URL").rstrip("/"),
            jira_base_url=_required(source, "JIRA_BASE_URL").rstrip("/"),
            endor_api_url=_required(source, "ENDOR_API_URL").rstrip("/"),
            endor_api_key=_required(source, "ENDOR_API_CREDENTIALS_KEY"),
            endor_api_secret=_required(source, "ENDOR_API_CREDENTIALS_SECRET"),
        )


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, remainder = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.match(key):
            raise ValueError(f"invalid .env line {number}")
        values[key] = _parse_value(remainder.strip(), number)
    return values


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _parse_value(raw: str, number: int) -> str:
    if not raw:
        return ""
    if raw[0] not in {'"', "'"}:
        return raw
    quote = raw[0]
    chars: list[str] = []
    index = 1
    while index < len(raw):
        char = raw[index]
        if char == quote:
            return "".join(chars)
        if quote == '"' and char == "\\" and index + 1 < len(raw):
            escaped = raw[index + 1]
            chars.append({"n": "\n", "t": "\t", "r": "\r"}.get(escaped, escaped))
            index += 2
            continue
        chars.append(char)
        index += 1
    raise ValueError(f"unterminated quote on .env line {number}")


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _parse_listen(addr: str) -> tuple[str, int]:
    if addr.startswith(":"):
        return "", int(addr[1:])
    if ":" in addr:
        host, port = addr.rsplit(":", 1)
        return host, int(port)
    return "", int(addr)
