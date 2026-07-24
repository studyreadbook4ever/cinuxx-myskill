#!/usr/bin/env python3
"""One-file semantic change exhibits for Codex, Claude, and ciduxx."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import re
import shlex
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ciduxx_core import (
    CiduxxError,
    StateError,
    atomic_write_text,
    default_state_root,
    utc_now,
)


EXHIBIT_SCHEMA_VERSION = 1
EXHIBIT_KIND = "ciduxx.semantic-change-exhibit"
DEFAULT_EXHIBIT_NAME = "AI_CHANGELOG.html"
DATA_BEGIN = "<!-- CIDUXX:DATA:BEGIN v1 -->"
DATA_END = "<!-- CIDUXX:DATA:END -->"
SKIN_BEGIN = "<!-- CIDUXX:SKIN:BEGIN v1 -->"
SKIN_END = "<!-- CIDUXX:SKIN:END -->"
MAX_DOCUMENT_BYTES = 20_000_000
MAX_PAYLOAD_BYTES = 1_000_000
MAX_TEXT_CHARS = 100_000
MAX_TITLE_CHARS = 300
MAX_CHANGES = 100
MAX_SKIN_BYTES = 250_000
ID_RE = re.compile(r"^[a-z]+-[0-9a-f]{12}$")
CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SUPPORTED_TURN_STATUSES = {"pending", "answered", "partial"}


class ExhibitError(CiduxxError):
    """The exhibit file or operation violates the public format contract."""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ExhibitError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExhibitError(f"{field} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ExhibitError(f"{field} must include a timezone")
    return value


def _plain_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExhibitError(f"{field} must be an integer >= {minimum}")
    return value


def _identifier(value: Any, field: str, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not ID_RE.fullmatch(value)
        or not value.startswith(f"{prefix}-")
    ):
        raise ExhibitError(f"{field} must match {prefix}-[0-9a-f]{{12}}")
    return value


def _text(
    value: Any,
    field: str,
    *,
    maximum: int = MAX_TEXT_CHARS,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ExhibitError(f"{field} must be text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise ExhibitError(f"{field} must not contain NUL")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExhibitError(f"{field} must be valid UTF-8 text") from exc
    if not allow_empty and not normalized.strip():
        raise ExhibitError(f"{field} cannot be empty")
    if len(normalized) > maximum:
        raise ExhibitError(f"{field} exceeds {maximum:,} characters")
    return normalized


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ExhibitError(
            f"{field} fields differ from schema: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )


def _validate_agent(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ExhibitError(f"{field} must be an object")
    _exact_keys(value, {"client", "display_name"}, field)
    raw_client = _text(value["client"], f"{field}.client", maximum=64)
    client = raw_client.lower()
    if raw_client != client:
        raise ExhibitError(f"{field}.client must already be lowercase")
    if not CLIENT_RE.fullmatch(client):
        raise ExhibitError(
            f"{field}.client must use lowercase letters, digits, dots, dashes, or underscores"
        )
    display_name = _text(
        value["display_name"], f"{field}.display_name", maximum=100
    )
    return {"client": client, "display_name": display_name}


def validate_document(value: Any, *, require_answered: bool = False) -> dict[str, Any]:
    """Validate and normalize the canonical embedded exhibit document."""

    if not isinstance(value, dict):
        raise ExhibitError("exhibit data must be a JSON object")
    _exact_keys(value, {"schema_version", "kind", "document", "turns"}, "root")
    schema_version = _plain_int(value.get("schema_version"), "schema_version")
    if schema_version != EXHIBIT_SCHEMA_VERSION:
        raise ExhibitError(
            f"unsupported schema_version: {value.get('schema_version')!r}"
        )
    if value.get("kind") != EXHIBIT_KIND:
        raise ExhibitError(f"unsupported exhibit kind: {value.get('kind')!r}")

    document = value.get("document")
    if not isinstance(document, dict):
        raise ExhibitError("document must be an object")
    _exact_keys(
        document,
        {
            "id",
            "title",
            "created_at",
            "updated_at",
            "revision",
            "skin",
        },
        "document",
    )
    _identifier(document.get("id"), "document.id", "doc")
    _text(document.get("title"), "document.title", maximum=MAX_TITLE_CHARS)
    _timestamp(document.get("created_at"), "document.created_at")
    _timestamp(document.get("updated_at"), "document.updated_at")
    _plain_int(document.get("revision"), "document.revision")
    skin = document.get("skin")
    if not isinstance(skin, dict):
        raise ExhibitError("document.skin must be an object")
    _exact_keys(skin, {"name"}, "document.skin")
    _skin_name(skin.get("name"))

    turns = value.get("turns")
    if not isinstance(turns, list):
        raise ExhibitError("turns must be an array")
    seen_ids = {document["id"]}
    seen_idempotency: set[str] = set()
    for index, turn in enumerate(turns, start=1):
        field = f"turns[{index - 1}]"
        if not isinstance(turn, dict):
            raise ExhibitError(f"{field} must be an object")
        _exact_keys(
            turn,
            {
                "id",
                "sequence",
                "status",
                "requested_at",
                "answered_at",
                "request",
                "agent",
                "idempotency_key",
                "changes",
            },
            field,
        )
        turn_id = _identifier(turn.get("id"), f"{field}.id", "turn")
        if turn_id in seen_ids:
            raise ExhibitError(f"duplicate id: {turn_id}")
        seen_ids.add(turn_id)
        if _plain_int(turn.get("sequence"), f"{field}.sequence", 1) != index:
            raise ExhibitError(f"{field}.sequence must be {index}")
        status_value = turn.get("status")
        if status_value not in SUPPORTED_TURN_STATUSES:
            raise ExhibitError(f"{field}.status is invalid")
        _timestamp(turn.get("requested_at"), f"{field}.requested_at")
        answered_at = turn.get("answered_at")
        if status_value == "pending":
            if answered_at is not None:
                raise ExhibitError(f"{field}.answered_at must be null while pending")
        else:
            _timestamp(answered_at, f"{field}.answered_at")
        request = turn.get("request")
        if not isinstance(request, dict):
            raise ExhibitError(f"{field}.request must be an object")
        _exact_keys(request, {"text", "redacted"}, f"{field}.request")
        _text(request.get("text"), f"{field}.request.text")
        if not isinstance(request.get("redacted"), bool):
            raise ExhibitError(f"{field}.request.redacted must be boolean")
        _validate_agent(turn.get("agent"), f"{field}.agent")

        idempotency_key = turn.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = _text(
                idempotency_key, f"{field}.idempotency_key", maximum=300
            )
            if idempotency_key in seen_idempotency:
                raise ExhibitError(f"duplicate idempotency_key: {idempotency_key}")
            seen_idempotency.add(idempotency_key)

        changes = turn.get("changes")
        if not isinstance(changes, list):
            raise ExhibitError(f"{field}.changes must be an array")
        if len(changes) > MAX_CHANGES:
            raise ExhibitError(f"{field}.changes exceeds {MAX_CHANGES} items")
        if status_value == "pending" and changes:
            raise ExhibitError(f"{field}.changes must be empty while pending")
        if status_value != "pending" and not changes:
            raise ExhibitError(f"{field}.changes requires at least one item")
        if require_answered and status_value == "pending":
            raise ExhibitError(f"{field} is still pending")
        for change_index, change in enumerate(changes, start=1):
            change_field = f"{field}.changes[{change_index - 1}]"
            if not isinstance(change, dict):
                raise ExhibitError(f"{change_field} must be an object")
            _exact_keys(
                change, {"id", "sequence", "text", "recorded_at"}, change_field
            )
            change_id = _identifier(change.get("id"), f"{change_field}.id", "chg")
            if change_id in seen_ids:
                raise ExhibitError(f"duplicate id: {change_id}")
            seen_ids.add(change_id)
            if (
                _plain_int(
                    change.get("sequence"), f"{change_field}.sequence", 1
                )
                != change_index
            ):
                raise ExhibitError(
                    f"{change_field}.sequence must be {change_index}"
                )
            _text(change.get("text"), f"{change_field}.text")
            _timestamp(change.get("recorded_at"), f"{change_field}.recorded_at")
    return value


def new_document(title: str, *, skin_name: str = "default") -> dict[str, Any]:
    title = _text(title, "title", maximum=MAX_TITLE_CHARS)
    skin_name = _skin_name(skin_name)
    now = utc_now()
    return {
        "schema_version": EXHIBIT_SCHEMA_VERSION,
        "kind": EXHIBIT_KIND,
        "document": {
            "id": _new_id("doc"),
            "title": title,
            "created_at": now,
            "updated_at": now,
            "revision": 0,
            "skin": {"name": skin_name},
        },
        "turns": [],
    }


def _safe_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _extract_marked(text: str, begin: str, end: str, field: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ExhibitError(f"{field} markers are missing or duplicated")
    start = text.index(begin)
    finish = text.index(end, start)
    if finish <= start:
        raise ExhibitError(f"{field} markers are out of order")
    return text[start : finish + len(end)]


def _extract_data(text: str) -> dict[str, Any]:
    block = _extract_marked(text, DATA_BEGIN, DATA_END, "data")
    match = re.fullmatch(
        re.escape(DATA_BEGIN)
        + r'\s*<script id="ciduxx-exhibit-data" type="application/json">\s*'
        + r"(?P<data>.*?)\s*</script>\s*"
        + re.escape(DATA_END),
        block,
        flags=re.DOTALL,
    )
    if match is None:
        raise ExhibitError("data block has an unsupported structure")
    try:
        value = json.loads(match.group("data"))
    except json.JSONDecodeError as exc:
        raise ExhibitError(f"embedded exhibit JSON is invalid: {exc}") from exc
    return validate_document(value)


def _default_skin_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "exhibit-skins" / "default.css"


def _validate_skin_css(css: Any, *, normalize: bool = True) -> str:
    if not isinstance(css, str):
        raise ExhibitError("skin must be text")
    try:
        css_bytes = css.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExhibitError("skin must be valid UTF-8") from exc
    if len(css_bytes) > MAX_SKIN_BYTES:
        raise ExhibitError(f"skin exceeds {MAX_SKIN_BYTES:,} bytes")
    lowered = css.lower()
    if "\x00" in css:
        raise ExhibitError("skin must not contain NUL")
    if "</style" in lowered:
        raise ExhibitError("skin must not contain a closing style tag")
    for marker in (SKIN_BEGIN, SKIN_END, DATA_BEGIN, DATA_END):
        if marker in css:
            raise ExhibitError("skin must not contain ciduxx format markers")
    return css.rstrip() + "\n" if normalize else css


def _skin_name(value: Any) -> str:
    name = _text(value, "skin name", maximum=100)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise ExhibitError("skin name must not contain control characters")
    return name


def _read_skin(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExhibitError(f"cannot read skin {path}: {exc}") from exc
    if len(raw) > MAX_SKIN_BYTES:
        raise ExhibitError(f"skin exceeds {MAX_SKIN_BYTES:,} bytes")
    try:
        css = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExhibitError("skin must be UTF-8") from exc
    return _validate_skin_css(css)


def _skin_block(css: str, name: str) -> str:
    css = _validate_skin_css(css)
    safe_name = html.escape(_skin_name(name), quote=True)
    return (
        f"{SKIN_BEGIN}\n"
        f'<style id="ciduxx-exhibit-skin" data-ciduxx-skin="{safe_name}">\n'
        f"{css}"
        "</style>\n"
        f"{SKIN_END}"
    )


def _validated_skin_block(text: str, expected_name: str) -> str:
    block = _extract_marked(text, SKIN_BEGIN, SKIN_END, "skin")
    match = re.fullmatch(
        re.escape(SKIN_BEGIN)
        + r'\n<style id="ciduxx-exhibit-skin" data-ciduxx-skin="(?P<name>[^"]*)">\n'
        + r"(?P<css>.*?)</style>\n"
        + re.escape(SKIN_END),
        block,
        flags=re.DOTALL,
    )
    if match is None:
        raise ExhibitError("skin block has an unsupported structure")
    expected_attribute = html.escape(_skin_name(expected_name), quote=True)
    if match.group("name") != expected_attribute:
        raise ExhibitError("skin block name does not match canonical exhibit data")
    _validate_skin_css(match.group("css"), normalize=False)
    return block


def _message_text(value: str) -> str:
    return html.escape(value, quote=False)


def _render_turn(turn: Mapping[str, Any]) -> str:
    status_value = html.escape(str(turn["status"]), quote=True)
    turn_id = html.escape(str(turn["id"]), quote=True)
    requested_at = html.escape(str(turn["requested_at"]), quote=True)
    request_text = _message_text(str(turn["request"]["text"]))
    redacted = " · redacted" if turn["request"]["redacted"] else ""
    agent_name = _message_text(str(turn["agent"]["display_name"]))
    agent_status = " · partial" if turn["status"] == "partial" else ""
    sections = [
        (
            f'  <article class="ciduxx-turn" data-turn-id="{turn_id}" '
            f'data-status="{status_value}">'
        ),
        '    <div class="ciduxx-message-row ciduxx-message-row--request">',
        (
            f'      <section class="ciduxx-message ciduxx-message--request" '
            f'data-role="request" aria-label="Human request">'
        ),
        '        <div class="ciduxx-message__meta">You'
        f"{html.escape(redacted)}</div>",
        f'        <div class="ciduxx-message__text">{request_text}</div>',
        (
            f'        <time class="ciduxx-message__time" '
            f'datetime="{requested_at}">{requested_at}</time>'
        ),
        "      </section>",
        "    </div>",
    ]
    if turn["status"] == "pending":
        sections.extend(
            [
                '    <div class="ciduxx-message-row ciduxx-message-row--change">',
                (
                    '      <section class="ciduxx-message '
                    'ciduxx-message--change ciduxx-message--pending" '
                    'data-role="pending" aria-label="Pending response">'
                ),
                f'        <div class="ciduxx-message__meta">{agent_name}</div>',
                (
                    '        <div class="ciduxx-message__text">'
                    "Verified changes have not been recorded yet."
                    "</div>"
                ),
                "      </section>",
                "    </div>",
            ]
        )
    else:
        answered_at = html.escape(str(turn["answered_at"]), quote=True)
        for change in turn["changes"]:
            change_id = html.escape(str(change["id"]), quote=True)
            change_text = _message_text(str(change["text"]))
            sections.extend(
                [
                    '    <div class="ciduxx-message-row ciduxx-message-row--change">',
                    (
                        f'      <section class="ciduxx-message '
                        f'ciduxx-message--change" data-role="change" '
                        f'data-change-id="{change_id}" '
                        f'aria-label="Implemented change">'
                    ),
                    (
                        f'        <div class="ciduxx-message__meta">'
                        f"{agent_name}{agent_status}</div>"
                    ),
                    f'        <div class="ciduxx-message__text">{change_text}</div>',
                    (
                        f'        <time class="ciduxx-message__time" '
                        f'datetime="{answered_at}">{answered_at}</time>'
                    ),
                    "      </section>",
                    "    </div>",
                ]
            )
    sections.extend(["  </article>"])
    return "\n".join(sections)


def render_document(
    value: Mapping[str, Any], *, preserved_skin_block: str | None = None
) -> str:
    data = validate_document(dict(value))
    document = data["document"]
    if preserved_skin_block is None:
        css = _read_skin(_default_skin_path())
        preserved_skin_block = _skin_block(css, document["skin"]["name"])
    else:
        preserved_skin_block = _validated_skin_block(
            preserved_skin_block, document["skin"]["name"]
        )
    title = html.escape(str(document["title"]))
    updated_at = html.escape(str(document["updated_at"]), quote=True)
    turn_count = len(data["turns"])
    if data["turns"]:
        rendered_turns = "\n".join(_render_turn(turn) for turn in data["turns"])
    else:
        rendered_turns = (
            '  <section class="ciduxx-empty" data-role="empty">'
            "No semantic change conversations have been recorded yet."
            "</section>"
        )
    embedded = _safe_json(data)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="generator" content="ciduxx exhibit v1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'">
  <title>{title}</title>
{preserved_skin_block}
</head>
<body>
  <header class="ciduxx-header">
    <p class="ciduxx-eyebrow">Semantic change exhibit</p>
    <h1 class="ciduxx-title">{title}</h1>
    <p class="ciduxx-subtitle"><span data-role="turn-count">{turn_count}</span> conversation turn(s)</p>
  </header>
<main class="ciduxx-chat" data-role="conversation" data-schema-version="1">
{rendered_turns}
</main>
  <footer class="ciduxx-footer">
    Last updated <time datetime="{updated_at}">{updated_at}</time>
  </footer>
{DATA_BEGIN}
<script id="ciduxx-exhibit-data" type="application/json">
{embedded}
</script>
{DATA_END}
</body>
</html>
"""


def _read_html(path: Path) -> tuple[dict[str, Any], str, str]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ExhibitError(f"exhibit does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ExhibitError(f"exhibit target must be a regular file: {path}")
    if info.st_size > MAX_DOCUMENT_BYTES:
        raise ExhibitError(f"exhibit exceeds {MAX_DOCUMENT_BYTES:,} bytes")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExhibitError(f"cannot read UTF-8 exhibit {path}: {exc}") from exc
    data = _extract_data(text)
    skin = _validated_skin_block(text, data["document"]["skin"]["name"])
    return data, skin, text


def read_exhibit(path: Path, *, require_answered: bool = False) -> dict[str, Any]:
    data, _, _ = _read_html(path)
    return validate_document(data, require_answered=require_answered)


def _git_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.exists() or marker.is_symlink():
            return candidate
    return current


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink_chain(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current = current / component
        if current.is_symlink():
            raise ExhibitError(f"exhibit parent path must not contain symlinks: {current}")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ExhibitError(f"exhibit target must be a regular file: {path}")


def resolve_exhibit_path(
    value: str | os.PathLike[str] | None,
    *,
    workspace: Path | None = None,
    allow_outside_workspace: bool = False,
) -> Path:
    base = (workspace or _git_root(Path.cwd())).resolve()
    candidate = Path(value or os.environ.get("CIDUXX_EXHIBIT_FILE", DEFAULT_EXHIBIT_NAME))
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    if candidate.suffix.lower() != ".html":
        raise ExhibitError("exhibit path must end in .html")
    if not allow_outside_workspace and not _is_relative_to(candidate, base):
        raise ExhibitError(
            f"exhibit path is outside workspace {base}; use the explicit outside override"
        )
    if not candidate.parent.is_dir():
        raise ExhibitError(f"exhibit parent does not exist: {candidate.parent}")
    _reject_symlink_chain(candidate)
    return candidate


@contextmanager
def exhibit_lock(path: Path) -> Iterator[None]:
    lock_root = default_state_root() / "exhibit-locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock_root.is_symlink() or not lock_root.is_dir():
        raise StateError(f"unsafe exhibit lock root: {lock_root}")
    try:
        lock_root.chmod(0o700)
    except OSError as exc:
        raise StateError(f"cannot secure exhibit lock root: {exc}") from exc
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise StateError(f"unsafe exhibit lock file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_html(path: Path, text: str) -> None:
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExhibitError("rendered exhibit is not valid UTF-8") from exc
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ExhibitError(f"rendered exhibit exceeds {MAX_DOCUMENT_BYTES:,} bytes")
    mode = 0o644
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    atomic_write_text(path, text, mode=mode)


def _agent(client: str, display_name: str | None) -> dict[str, str]:
    client = _text(client, "client", maximum=64).lower()
    if not CLIENT_RE.fullmatch(client):
        raise ExhibitError(
            "client must use lowercase letters, digits, dots, dashes, or underscores"
        )
    return {
        "client": client,
        "display_name": _text(
            display_name or client.replace("-", " ").title(),
            "display name",
            maximum=100,
        ),
    }


def _change_objects(changes: Sequence[str], recorded_at: str) -> list[dict[str, Any]]:
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
        raise ExhibitError("changes must be an array of text")
    if not changes:
        raise ExhibitError("at least one semantic change is required")
    if len(changes) > MAX_CHANGES:
        raise ExhibitError(f"changes exceeds {MAX_CHANGES} items")
    return [
        {
            "id": _new_id("chg"),
            "sequence": index,
            "text": _text(change, f"changes[{index - 1}]"),
            "recorded_at": recorded_at,
        }
        for index, change in enumerate(changes, start=1)
    ]


def _normalize_changes(changes: Any) -> list[str]:
    if not isinstance(changes, (list, tuple)):
        raise ExhibitError("changes must be an array of text")
    if not changes:
        raise ExhibitError("at least one semantic change is required")
    if len(changes) > MAX_CHANGES:
        raise ExhibitError(f"changes exceeds {MAX_CHANGES} items")
    return [
        _text(item, f"changes[{index}]") for index, item in enumerate(changes)
    ]


def _idempotency_match(
    turn: Mapping[str, Any],
    *,
    request: str,
    changes: Sequence[str],
    agent: Mapping[str, str],
    status_value: str,
    redacted: bool,
) -> bool:
    return (
        turn["request"] == {"text": request, "redacted": redacted}
        and turn["agent"] == dict(agent)
        and turn["status"] == status_value
        and [item["text"] for item in turn["changes"]] == list(changes)
    )


def init_exhibit(
    path: Path,
    *,
    title: str,
    skin_css: str | None = None,
    skin_name: str = "default",
) -> dict[str, Any]:
    with exhibit_lock(path):
        if path.exists():
            data, _, _ = _read_html(path)
            return {
                "created": False,
                "file": str(path),
                "document_id": data["document"]["id"],
                "revision": data["document"]["revision"],
            }
        data = new_document(title, skin_name=skin_name)
        skin = (
            _skin_block(skin_css, skin_name)
            if skin_css is not None
            else _skin_block(_read_skin(_default_skin_path()), skin_name)
        )
        _write_html(path, render_document(data, preserved_skin_block=skin))
        return {
            "created": True,
            "file": str(path),
            "document_id": data["document"]["id"],
            "revision": data["document"]["revision"],
        }


def begin_turn(
    path: Path,
    *,
    request: str,
    client: str,
    display_name: str | None = None,
    idempotency_key: str | None = None,
    redacted: bool = False,
    title: str | None = None,
) -> dict[str, Any]:
    request = _text(request, "request")
    agent = _agent(client, display_name)
    if not isinstance(redacted, bool):
        raise ExhibitError("redacted must be boolean")
    if idempotency_key is not None:
        idempotency_key = _text(
            idempotency_key, "idempotency key", maximum=300
        )
    with exhibit_lock(path):
        if path.exists():
            data, skin, _ = _read_html(path)
        else:
            data = new_document(title or f"{path.parent.name} AI Change Log")
            skin = _skin_block(_read_skin(_default_skin_path()), "default")
        if idempotency_key is not None:
            for turn in data["turns"]:
                if turn["idempotency_key"] == idempotency_key:
                    if (
                        turn["request"] == {"text": request, "redacted": redacted}
                        and turn["agent"] == agent
                    ):
                        return {
                            "created": False,
                            "file": str(path),
                            "turn_id": turn["id"],
                            "revision": data["document"]["revision"],
                            "status": turn["status"],
                        }
                    raise ExhibitError(
                        "idempotency key already exists with different content"
                    )
        now = utc_now()
        turn = {
            "id": _new_id("turn"),
            "sequence": len(data["turns"]) + 1,
            "status": "pending",
            "requested_at": now,
            "answered_at": None,
            "request": {"text": request, "redacted": bool(redacted)},
            "agent": agent,
            "idempotency_key": idempotency_key,
            "changes": [],
        }
        data["turns"].append(turn)
        data["document"]["revision"] += 1
        data["document"]["updated_at"] = now
        validate_document(data)
        _write_html(path, render_document(data, preserved_skin_block=skin))
        return {
            "created": True,
            "file": str(path),
            "turn_id": turn["id"],
            "revision": data["document"]["revision"],
            "status": turn["status"],
        }


def answer_turn(
    path: Path,
    *,
    turn_id: str,
    changes: Sequence[str],
    status_value: str = "answered",
) -> dict[str, Any]:
    if status_value not in {"answered", "partial"}:
        raise ExhibitError("answer status must be answered or partial")
    changes = _normalize_changes(changes)
    with exhibit_lock(path):
        data, skin, _ = _read_html(path)
        match = next((turn for turn in data["turns"] if turn["id"] == turn_id), None)
        if match is None:
            raise ExhibitError(f"turn does not exist: {turn_id}")
        if match["status"] != "pending":
            if (
                match["status"] == status_value
                and [item["text"] for item in match["changes"]] == list(changes)
            ):
                return {
                    "created": False,
                    "file": str(path),
                    "turn_id": turn_id,
                    "revision": data["document"]["revision"],
                    "status": match["status"],
                }
            raise ExhibitError("turn is already answered with different content")
        now = utc_now()
        match["status"] = status_value
        match["answered_at"] = now
        match["changes"] = _change_objects(changes, now)
        data["document"]["revision"] += 1
        data["document"]["updated_at"] = now
        validate_document(data)
        _write_html(path, render_document(data, preserved_skin_block=skin))
        return {
            "created": True,
            "file": str(path),
            "turn_id": turn_id,
            "revision": data["document"]["revision"],
            "status": match["status"],
        }


def record_turn(
    path: Path,
    *,
    request: str,
    changes: Sequence[str],
    client: str,
    display_name: str | None = None,
    idempotency_key: str | None = None,
    redacted: bool = False,
    title: str | None = None,
    status_value: str = "answered",
    update_partial: bool = False,
) -> dict[str, Any]:
    if status_value not in {"answered", "partial"}:
        raise ExhibitError("record status must be answered or partial")
    request = _text(request, "request")
    normalized_changes = _normalize_changes(changes)
    agent = _agent(client, display_name)
    if not isinstance(redacted, bool):
        raise ExhibitError("redacted must be boolean")
    if idempotency_key is not None:
        idempotency_key = _text(
            idempotency_key, "idempotency key", maximum=300
        )
    with exhibit_lock(path):
        if path.exists():
            data, skin, _ = _read_html(path)
        else:
            data = new_document(title or f"{path.parent.name} AI Change Log")
            skin = _skin_block(_read_skin(_default_skin_path()), "default")
        if idempotency_key is not None:
            for turn in data["turns"]:
                if turn["idempotency_key"] == idempotency_key:
                    if _idempotency_match(
                        turn,
                        request=request,
                        changes=normalized_changes,
                        agent=agent,
                        status_value=status_value,
                        redacted=bool(redacted),
                    ):
                        return {
                            "created": False,
                            "file": str(path),
                            "turn_id": turn["id"],
                            "revision": data["document"]["revision"],
                            "status": turn["status"],
                        }
                    if (
                        update_partial
                        and turn["status"] == "partial"
                        and status_value in {"partial", "answered"}
                        and turn["agent"] == agent
                        and turn["request"]["text"] == request
                    ):
                        now = utc_now()
                        updated_changes = normalized_changes
                        if status_value == "partial":
                            prior_texts = [
                                change["text"] for change in turn["changes"]
                            ]
                            updated_changes = [
                                *prior_texts,
                                *[
                                    change
                                    for change in normalized_changes
                                    if change not in prior_texts
                                ],
                            ]
                        turn["status"] = status_value
                        turn["answered_at"] = now
                        turn["changes"] = _change_objects(updated_changes, now)
                        turn["request"]["redacted"] = bool(
                            turn["request"]["redacted"] or redacted
                        )
                        data["document"]["revision"] += 1
                        data["document"]["updated_at"] = now
                        validate_document(data)
                        _write_html(
                            path,
                            render_document(data, preserved_skin_block=skin),
                        )
                        return {
                            "created": False,
                            "updated": True,
                            "file": str(path),
                            "turn_id": turn["id"],
                            "revision": data["document"]["revision"],
                            "status": turn["status"],
                        }
                    raise ExhibitError(
                        "idempotency key already exists with different content"
                    )
        now = utc_now()
        turn = {
            "id": _new_id("turn"),
            "sequence": len(data["turns"]) + 1,
            "status": status_value,
            "requested_at": now,
            "answered_at": now,
            "request": {"text": request, "redacted": bool(redacted)},
            "agent": agent,
            "idempotency_key": idempotency_key,
            "changes": _change_objects(normalized_changes, now),
        }
        data["turns"].append(turn)
        data["document"]["revision"] += 1
        data["document"]["updated_at"] = now
        validate_document(data)
        _write_html(path, render_document(data, preserved_skin_block=skin))
        return {
            "created": True,
            "updated": False,
            "file": str(path),
            "turn_id": turn["id"],
            "revision": data["document"]["revision"],
            "status": turn["status"],
        }


def apply_skin(path: Path, *, css: str, name: str) -> dict[str, Any]:
    css = _validate_skin_css(css)
    name = _skin_name(name)
    with exhibit_lock(path):
        data, _, _ = _read_html(path)
        now = utc_now()
        data["document"]["skin"]["name"] = name
        data["document"]["revision"] += 1
        data["document"]["updated_at"] = now
        skin = _skin_block(css, name)
        _write_html(path, render_document(data, preserved_skin_block=skin))
        return {
            "file": str(path),
            "skin": name,
            "revision": data["document"]["revision"],
        }


def rerender(path: Path) -> dict[str, Any]:
    with exhibit_lock(path):
        data, skin, original = _read_html(path)
        rendered = render_document(data, preserved_skin_block=skin)
        changed = rendered != original
        if changed:
            _write_html(path, rendered)
        return {
            "file": str(path),
            "changed": changed,
            "revision": data["document"]["revision"],
        }


def validate_exhibit(path: Path, *, require_answered: bool = False) -> dict[str, Any]:
    data, skin, original = _read_html(path)
    validate_document(data, require_answered=require_answered)
    if render_document(data, preserved_skin_block=skin) != original:
        raise ExhibitError(
            "generated presentation differs from canonical data; run exhibit render"
        )
    return {
        "valid": True,
        "file": str(path),
        "schema_version": data["schema_version"],
        "document_id": data["document"]["id"],
        "revision": data["document"]["revision"],
        "turns": len(data["turns"]),
        "pending": sum(turn["status"] == "pending" for turn in data["turns"]),
        "skin": data["document"]["skin"]["name"],
        "skin_sha256": hashlib.sha256(skin.encode("utf-8")).hexdigest(),
    }


def _payload(path_value: str) -> dict[str, Any]:
    try:
        if path_value == "-":
            raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        else:
            raw = Path(path_value).expanduser().read_bytes()
    except OSError as exc:
        raise ExhibitError(f"cannot read payload: {exc}") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ExhibitError(f"payload exceeds {MAX_PAYLOAD_BYTES:,} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExhibitError(f"payload must be a UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ExhibitError("payload must be a JSON object")
    return value


def _request_file(path_value: str) -> str:
    try:
        if path_value == "-":
            raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        else:
            raw = Path(path_value).expanduser().read_bytes()
    except OSError as exc:
        raise ExhibitError(f"cannot read request file: {exc}") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ExhibitError(f"request file exceeds {MAX_PAYLOAD_BYTES:,} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExhibitError("request file must be UTF-8") from exc


def _operation_values(args: argparse.Namespace, operation: str) -> dict[str, Any]:
    if args.payload:
        conflicts: list[str] = []
        if operation in {"record", "begin"}:
            for name in (
                "request",
                "request_file",
                "client",
                "display_name",
                "idempotency_key",
                "title",
            ):
                if getattr(args, name, None) is not None:
                    conflicts.append(f"--{name.replace('_', '-')}")
            if getattr(args, "redacted", False):
                conflicts.append("--redacted")
            if operation == "record" and getattr(args, "change", []):
                conflicts.append("--change")
            if operation == "record" and getattr(args, "status", "answered") != "answered":
                conflicts.append("--status")
        else:
            if getattr(args, "turn_id", None) is not None:
                conflicts.append("TURN_ID")
            if getattr(args, "change", []):
                conflicts.append("--change")
            if getattr(args, "status", "answered") != "answered":
                conflicts.append("--status")
        if conflicts:
            raise ExhibitError(
                "--payload cannot be combined with ordinary input fields: "
                + ", ".join(conflicts)
            )
        value = _payload(args.payload)
        if operation == "record":
            allowed = {
                "request",
                "changes",
                "client",
                "display_name",
                "idempotency_key",
                "redacted",
                "title",
                "status",
            }
            required = {"request", "changes"}
        elif operation == "begin":
            allowed = {
                "request",
                "client",
                "display_name",
                "idempotency_key",
                "redacted",
                "title",
            }
            required = {"request"}
        else:
            allowed = {"turn_id", "changes", "status"}
            required = {"turn_id", "changes"}
        unknown = set(value) - allowed
        missing = required - set(value)
        if unknown or missing:
            raise ExhibitError(
                f"{operation} payload fields invalid; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        return value

    if operation in {"record", "begin"}:
        if bool(args.request) == bool(args.request_file):
            raise ExhibitError("provide exactly one of --request or --request-file")
        request = args.request if args.request is not None else _request_file(args.request_file)
        value = {
            "request": request,
            "client": args.client or "codex",
            "display_name": args.display_name,
            "idempotency_key": args.idempotency_key,
            "redacted": bool(args.redacted),
            "title": args.title,
        }
        if operation == "record":
            value["changes"] = args.change
            value["status"] = args.status
        return value
    return {
        "turn_id": args.turn_id,
        "changes": args.change,
        "status": args.status,
    }


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _instructions(agent: str, file_name: str) -> str:
    display = "Claude Code" if agent == "claude" else "Codex"
    markdown_file = file_name.replace("`", "\\`")
    shell_file = shlex.quote(file_name)
    return f"""# Semantic change exhibit instructions for {display}

When `{markdown_file}` exists, treat it as the project's semantic change exhibit.

For each actionable human modification request:

1. Preserve a display-safe version of the request. Never include secrets, hidden prompts,
   chain-of-thought, raw tool logs, or patch hunks.
2. Implement and verify the requested work.
3. If at least one real user-visible or behaviorally meaningful change was made, run:

```sh
ciduxx exhibit record --file {shell_file} --payload -
```

Pass one UTF-8 JSON object on stdin:

```json
{{
  "request": "The human modification request",
  "changes": [
    "Changed the outcome in plain language.",
    "Added another verified user-facing improvement."
  ],
  "client": "{agent}",
  "display_name": "{display}",
  "idempotency_key": "stable-task-or-message-id"
}}
```

Use outcome language such as "Changed X to Y." Do not describe code diffs. Group all
changes caused by one request in the same `changes` array. Do not record questions,
unchanged work, guesses, or failed attempts. Use `"redacted": true` and a safe paraphrase
when the original request contains sensitive values.
"""


def _add_file_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--file",
        help=f"Exhibit HTML path (default: Git root/{DEFAULT_EXHIBIT_NAME}).",
    )
    parser.add_argument(
        "--allow-outside-workspace",
        action="store_true",
        help="Explicitly permit an exhibit path outside the current Git workspace.",
    )


def _add_request_arguments(parser: argparse.ArgumentParser, *, changes: bool) -> None:
    parser.add_argument("--payload", help="Read structured JSON from FILE or - for stdin.")
    parser.add_argument("--request", help="Display-safe human modification request.")
    parser.add_argument("--request-file", help="Read request text from FILE or - for stdin.")
    if changes:
        parser.add_argument(
            "--change",
            action="append",
            default=[],
            help="Verified semantic change; repeat for multiple changes.",
        )
        parser.add_argument(
            "--status",
            choices=("answered", "partial"),
            default="answered",
            help="Mark the recorded outcomes complete or durably partial.",
        )
    parser.add_argument("--client", help="Agent client name, such as codex or claude.")
    parser.add_argument("--display-name", help="Human-facing agent label.")
    parser.add_argument(
        "--idempotency-key",
        help="Stable key that makes an identical retry return the existing turn.",
    )
    parser.add_argument(
        "--redacted",
        action="store_true",
        help="Label the supplied request as an already-safe redacted paraphrase.",
    )
    parser.add_argument("--title", help="Title used only when creating a missing exhibit.")


def configure_exhibit_parser(subparsers: Any) -> None:
    exhibit = subparsers.add_parser(
        "exhibit",
        help="Maintain a one-file semantic request/change HTML conversation.",
    )
    actions = exhibit.add_subparsers(dest="exhibit_action", required=True)

    init = actions.add_parser("init", help="Create an empty standalone HTML exhibit.")
    _add_file_arguments(init)
    init.add_argument("--title")
    init.add_argument("--skin-file")
    init.add_argument("--skin-name", default="default")

    begin = actions.add_parser(
        "begin", help="Store one human request as a pending conversation turn."
    )
    _add_file_arguments(begin)
    _add_request_arguments(begin, changes=False)

    answer = actions.add_parser(
        "answer", help="Attach one or more semantic changes to a pending turn."
    )
    _add_file_arguments(answer)
    answer.add_argument(
        "turn_id",
        nargs="?",
        metavar="TURN_ID",
        help="Pending turn ID; omit only when --payload supplies turn_id.",
    )
    answer.add_argument("--payload", help="Read structured JSON from FILE or - for stdin.")
    answer.add_argument(
        "--change",
        action="append",
        default=[],
        help="Verified semantic change; repeat for multiple changes.",
    )
    answer.add_argument(
        "--status",
        choices=("answered", "partial"),
        default="answered",
        help="Mark the recorded outcomes complete or durably partial.",
    )

    record = actions.add_parser(
        "record",
        help="Atomically append one request and one-or-more semantic changes.",
    )
    _add_file_arguments(record)
    _add_request_arguments(record, changes=True)

    skin = actions.add_parser(
        "skin", help="Replace only the embedded CSS skin, preserving conversation data."
    )
    _add_file_arguments(skin)
    source = skin.add_mutually_exclusive_group(required=True)
    source.add_argument("--css-file")
    source.add_argument("--builtin", choices=("default",))
    skin.add_argument("--name")

    render = actions.add_parser(
        "render", help="Regenerate static chat HTML from canonical embedded JSON."
    )
    _add_file_arguments(render)

    validate = actions.add_parser(
        "validate", help="Validate schema, markers, turns, and skin structure."
    )
    _add_file_arguments(validate)
    validate.add_argument("--require-answered", action="store_true")

    instructions = actions.add_parser(
        "instructions",
        help="Print a portable agent instruction snippet for Codex or Claude.",
    )
    instructions.add_argument("--agent", choices=("codex", "claude"), required=True)
    instructions.add_argument(
        "--file",
        default=DEFAULT_EXHIBIT_NAME,
        help=f"Exhibit path to show in the generated prompt (default: {DEFAULT_EXHIBIT_NAME}).",
    )

    for action in actions.choices.values():
        action.set_defaults(handler=command_exhibit)


def command_exhibit(args: argparse.Namespace) -> int:
    if args.exhibit_action == "instructions":
        print(_instructions(args.agent, args.file))
        return 0
    path = resolve_exhibit_path(
        args.file, allow_outside_workspace=args.allow_outside_workspace
    )
    if args.exhibit_action == "init":
        css = _read_skin(Path(args.skin_file).expanduser()) if args.skin_file else None
        result = init_exhibit(
            path,
            title=args.title or f"{path.parent.name} AI Change Log",
            skin_css=css,
            skin_name=args.skin_name,
        )
    elif args.exhibit_action == "begin":
        value = _operation_values(args, "begin")
        result = begin_turn(
            path,
            request=value["request"],
            client=value.get("client") or "codex",
            display_name=value.get("display_name"),
            idempotency_key=value.get("idempotency_key"),
            redacted=value.get("redacted", False),
            title=value.get("title"),
        )
    elif args.exhibit_action == "answer":
        value = _operation_values(args, "answer")
        result = answer_turn(
            path,
            turn_id=value["turn_id"],
            changes=value["changes"],
            status_value=value.get("status", "answered"),
        )
    elif args.exhibit_action == "record":
        value = _operation_values(args, "record")
        result = record_turn(
            path,
            request=value["request"],
            changes=value["changes"],
            client=value.get("client") or "codex",
            display_name=value.get("display_name"),
            idempotency_key=value.get("idempotency_key"),
            redacted=value.get("redacted", False),
            title=value.get("title"),
            status_value=value.get("status", "answered"),
        )
    elif args.exhibit_action == "skin":
        if args.builtin == "default":
            css = _read_skin(_default_skin_path())
            name = args.name or "default"
        else:
            css = _read_skin(Path(args.css_file).expanduser())
            name = args.name or Path(args.css_file).stem
        result = apply_skin(path, css=css, name=name)
    elif args.exhibit_action == "render":
        result = rerender(path)
    elif args.exhibit_action == "validate":
        result = validate_exhibit(path, require_answered=args.require_answered)
    else:
        raise ExhibitError(f"unknown exhibit action: {args.exhibit_action}")
    _print_json(result)
    return 0
