#!/usr/bin/env python3
"""Trusted state, artifact, and Linux power primitives for ciduxx."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
GROUP_ID_RE = re.compile(r"^g-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
MEMBER_ID_RE = re.compile(r"^m-[0-9a-f]{12}$")
ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TERMINAL_MEMBER_STATUSES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "limit",
    "cancelled",
}
FINALIZED_POWER_STATUSES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "limit",
}
SHUTDOWN_POLICIES = {"never", "completed", "finalized"}
GROUP_FINAL_STATES = {
    "complete",
    "ineligible",
    "armed",
    "power_failed",
    "cancelled",
    "arming_unknown",
}
GROUP_STATUSES = GROUP_FINAL_STATES | {"open", "sealed", "arming"}
POWER_ATTEMPT_STATUSES = {"arming", "armed", "power_failed", "arming_unknown"}


class CiduxxError(RuntimeError):
    """Base error with a stable CLI-facing message."""


class StateError(CiduxxError):
    """Authoritative state is missing, corrupt, or violates the protocol."""


class UnsafeEnvironment(CiduxxError):
    """Host power operations are unsafe in the current environment."""


class PowerError(CiduxxError):
    """The one-shot power adapter could not schedule shutdown."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def boot_id() -> str:
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        value = "unknown"
    return value or "unknown"


def default_state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        base = Path(xdg).expanduser()
        if not base.is_absolute():
            raise StateError("XDG_STATE_HOME must be an absolute path")
        return base / "ciduxx"
    return Path.home() / ".local" / "state" / "ciduxx"


def trusted_power_state_root() -> Path:
    """Return the real-power anchor without trusting caller-controlled env vars."""

    try:
        account_home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise StateError("current uid has no operating-system account home") from exc
    if not isinstance(account_home, str) or not account_home or "\x00" in account_home:
        raise StateError("current uid has an invalid operating-system account home")
    home = Path(account_home)
    if not home.is_absolute():
        raise StateError("operating-system account home must be absolute")
    return Path(os.path.abspath(os.fspath(home / ".local" / "state" / "ciduxx")))


def _validate_component(value: str, pattern: re.Pattern[str], kind: str) -> str:
    if not pattern.fullmatch(value):
        raise StateError(f"invalid {kind}: {value!r}")
    return value


def _ensure_private_dir(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise StateError(f"state path must be absolute: {path}")
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise StateError(f"state path must not contain symlinks: {current}")
            if not stat.S_ISDIR(info.st_mode):
                raise StateError(f"state path component is not a directory: {current}")
        else:
            current.mkdir(mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StateError(f"state path is not a real directory: {path}")
    if info.st_uid != os.geteuid():
        raise StateError(f"state path is not owned by uid {os.geteuid()}: {path}")
    if info.st_mode & 0o077:
        path.chmod(0o700)
    return path


def _validate_trusted_directory_chain(path: Path) -> None:
    """Require a symlink-free, non-writable ancestry for real power state."""

    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    if not path.is_absolute():
        raise UnsafeEnvironment(f"trusted state path must be absolute: {path}")
    allowed_owners = {0, os.geteuid()}
    current = Path(path.anchor)
    components = [current]
    for component in path.parts[1:]:
        current = current / component
        components.append(current)
    for current in components:
        try:
            info = current.lstat()
        except OSError as exc:
            raise UnsafeEnvironment(
                f"cannot inspect trusted state path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeEnvironment(
                f"trusted state path component is not a real directory: {current}"
            )
        if info.st_uid not in allowed_owners:
            raise UnsafeEnvironment(
                f"trusted state path component has unexpected owner: {current}"
            )
        if info.st_mode & 0o022:
            raise UnsafeEnvironment(
                f"trusted state path component is group/world writable: {current}"
            )


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_boot_id(value: Any) -> bool:
    return isinstance(value, str) and bool(BOOT_ID_RE.fullmatch(value))


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace a file atomically without following an existing target symlink."""

    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise StateError(f"unsafe output parent: {parent}")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StateError(f"refusing non-regular output target: {path}")
        if info.st_uid != os.geteuid():
            raise StateError(f"refusing output owned by another uid: {path}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Mapping[str, Any], mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def load_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StateError(f"state file is not regular: {path}")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError as exc:
        raise StateError(f"state file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read valid state from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"state root must be an object: {path}")
    return value


def process_start_time(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = raw[raw.rfind(")") + 2 :].split()
        return suffix[19]
    except (OSError, IndexError):
        return None


def process_is_same(pid: int, expected_start: str | None) -> bool:
    if expected_start is None:
        return False
    return process_start_time(pid) == expected_start


@dataclasses.dataclass(frozen=True)
class PowerIntent:
    group_id: str
    attempt_id: str
    delay_minutes: int
    reason: str
    allow_remote: bool
    allow_multi_user: bool


@dataclasses.dataclass(frozen=True)
class PowerResult:
    success: bool
    detail: str
    argv: tuple[str, ...] = ()
    uncertain: bool = False


def _validate_power_ledger(ledger: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "boot_id",
        "owner_uid",
        "group_id",
        "attempt_id",
        "status",
        "created_at",
        "updated_at",
        "detail",
    }
    allowed = required | {"argv"}
    if set(ledger) - allowed or not required.issubset(ledger):
        raise StateError("power ledger fields do not match the current schema")
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise StateError("power ledger has an unsupported schema")
    if not _valid_boot_id(ledger.get("boot_id")):
        raise StateError("power ledger has an invalid boot id")
    owner_uid = ledger.get("owner_uid")
    if not _is_plain_int(owner_uid) or owner_uid != os.geteuid():
        raise StateError("power ledger has an unexpected owner uid")
    group_id = ledger.get("group_id")
    if not isinstance(group_id, str) or not GROUP_ID_RE.fullmatch(group_id):
        raise StateError("power ledger has an invalid group id")
    attempt_id = ledger.get("attempt_id")
    if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise StateError("power ledger has an invalid attempt id")
    status_value = ledger.get("status")
    if status_value not in POWER_ATTEMPT_STATUSES:
        raise StateError("power ledger has an invalid status")
    if not _is_timestamp(ledger.get("created_at")) or not _is_timestamp(
        ledger.get("updated_at")
    ):
        raise StateError("power ledger has invalid timestamps")
    detail = ledger.get("detail")
    if not isinstance(detail, str) or len(detail) > 4000:
        raise StateError("power ledger has invalid detail")
    if status_value == "arming":
        if "argv" in ledger:
            raise StateError("arming power ledger has a result argv")
    else:
        argv = ledger.get("argv")
        if (
            not isinstance(argv, list)
            or len(argv) > 16
            or any(not isinstance(item, str) or len(item) > 1000 for item in argv)
        ):
            raise StateError("power ledger has invalid result argv")


class GroupStore:
    """Own group state and serialize transitions with per-group and global locks."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = _ensure_private_dir(root or default_state_root())
        self.groups_root = _ensure_private_dir(self.root / "groups")
        self.locks_root = _ensure_private_dir(self.root / "locks")
        self.runs_root = _ensure_private_dir(self.root / "runs")

    def assert_real_power_ready(self) -> None:
        """Prove that real power state is anchored in the trusted default tree."""

        expected = trusted_power_state_root()
        if self.root != expected:
            raise UnsafeEnvironment(
                f"real power operations require the fixed trusted state root {expected}"
            )
        for path in (self.root, self.groups_root, self.locks_root, self.runs_root):
            _validate_trusted_directory_chain(path)
        current_boot = boot_id()
        if not _valid_boot_id(current_boot):
            raise UnsafeEnvironment(
                "cannot prove a valid Linux boot id for a real power operation"
            )

    def _group_dir(self, group_id: str) -> Path:
        _validate_component(group_id, GROUP_ID_RE, "group id")
        path = self.groups_root / group_id
        if path.exists():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise StateError(f"unsafe group directory: {path}")
            if info.st_uid != os.geteuid():
                raise StateError(f"group directory has unexpected owner: {path}")
        return path

    def _state_path(self, group_id: str) -> Path:
        return self._group_dir(group_id) / "group.json"

    @contextmanager
    def _lock(self, group_id: str) -> Iterator[None]:
        group_dir = self._group_dir(group_id)
        if not group_dir.is_dir():
            raise StateError(f"unknown group: {group_id}")
        lock_path = group_dir / ".lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def power_lock(self) -> Iterator[None]:
        path = self.locks_root / "power.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def create(
        self,
        *,
        name: str,
        expected: int | None,
        shutdown_on: str,
        delay_minutes: int,
        allow_remote: bool = False,
        allow_multi_user: bool = False,
    ) -> dict[str, Any]:
        if shutdown_on not in SHUTDOWN_POLICIES:
            raise StateError(f"unknown shutdown policy: {shutdown_on}")
        if expected is not None and not 1 <= expected <= 256:
            raise StateError("expected session count must be between 1 and 256")
        if not 1 <= delay_minutes <= 60:
            raise StateError("shutdown delay must be between 1 and 60 minutes")
        if not name.strip() or len(name) > 120 or any(ord(ch) < 32 for ch in name):
            raise StateError("group name must be 1-120 printable characters")

        now = dt.datetime.now(dt.timezone.utc)
        group_id = f"g-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        group_dir = self._group_dir(group_id)
        group_dir.mkdir(mode=0o700)
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "group_id": group_id,
            "name": name.strip(),
            "owner_uid": os.geteuid(),
            "boot_id": boot_id(),
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "expected_members": expected,
            "sealed": False,
            "status": "open",
            "shutdown": {
                "policy": shutdown_on,
                "delay_minutes": delay_minutes,
                "allow_remote": bool(allow_remote),
                "allow_multi_user": bool(allow_multi_user),
                "attempt_id": None,
                "attempted_at": None,
                "detail": None,
            },
            "members": {},
        }
        atomic_write_json(group_dir / "group.json", state)
        return state

    def list(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for path in sorted(self.groups_root.glob("g-*")):
            if (
                not path.is_dir()
                or path.is_symlink()
                or not GROUP_ID_RE.fullmatch(path.name)
            ):
                continue
            try:
                groups.append(load_json(path / "group.json"))
            except StateError:
                groups.append({"group_id": path.name, "status": "corrupt"})
        return groups

    def read(self, group_id: str) -> dict[str, Any]:
        return load_json(self._state_path(group_id))

    def _read_locked(self, group_id: str) -> dict[str, Any]:
        state = load_json(self._state_path(group_id))
        self._validate_state(state, group_id)
        return state

    def _validate_state(self, state: Mapping[str, Any], group_id: str) -> None:
        allowed_fields = {
            "schema_version",
            "group_id",
            "name",
            "owner_uid",
            "boot_id",
            "created_at",
            "updated_at",
            "expected_members",
            "sealed",
            "sealed_at",
            "finalized_at",
            "status",
            "cancelled_at",
            "cancel_reason",
            "shutdown",
            "members",
        }
        unknown_fields = set(state) - allowed_fields
        if unknown_fields:
            raise StateError(
                f"group {group_id} has unknown fields: {sorted(unknown_fields)}"
            )
        if state.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"unsupported group state schema for {group_id}")
        if state.get("group_id") != group_id:
            raise StateError(f"group id mismatch in {group_id}")
        name = state.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or any(ord(ch) < 32 for ch in name)
        ):
            raise StateError(f"group {group_id} has an invalid name")
        owner_uid = state.get("owner_uid")
        if not _is_plain_int(owner_uid) or owner_uid != os.geteuid():
            raise StateError(f"group {group_id} belongs to another uid")
        state_boot = state.get("boot_id")
        if not isinstance(state_boot, str) or state_boot != boot_id():
            raise StateError(f"group {group_id} belongs to another Linux boot")
        if not _is_timestamp(state.get("created_at")) or not _is_timestamp(
            state.get("updated_at")
        ):
            raise StateError(f"group {group_id} has invalid lifecycle timestamps")

        expected = state.get("expected_members")
        if expected is not None and (
            not _is_plain_int(expected) or not 1 <= expected <= 256
        ):
            raise StateError(f"group {group_id} has invalid expected membership")
        sealed = state.get("sealed")
        if not isinstance(sealed, bool):
            raise StateError(f"group {group_id} has invalid sealed state")
        status_value = state.get("status")
        if status_value not in GROUP_STATUSES:
            raise StateError(f"group {group_id} has invalid status {status_value!r}")
        if status_value == "open" and sealed:
            raise StateError(f"group {group_id} is open but marked sealed")
        if status_value not in {"open", "cancelled"} and not sealed:
            raise StateError(f"group {group_id} status requires a sealed group")
        if sealed:
            if not _is_timestamp(state.get("sealed_at")):
                raise StateError(f"group {group_id} has no valid seal timestamp")
        elif "sealed_at" in state:
            raise StateError(f"unsealed group {group_id} has a seal timestamp")

        if status_value == "cancelled":
            reason = state.get("cancel_reason")
            if (
                not _is_timestamp(state.get("cancelled_at"))
                or not isinstance(reason, str)
                or len(reason) > 1000
            ):
                raise StateError(f"group {group_id} has invalid cancellation state")
        elif "cancelled_at" in state or "cancel_reason" in state:
            raise StateError(f"group {group_id} has stray cancellation fields")

        finalized_statuses = {"complete", "ineligible"} | POWER_ATTEMPT_STATUSES
        if status_value in finalized_statuses:
            if not _is_timestamp(state.get("finalized_at")):
                raise StateError(f"group {group_id} has no valid finalization time")
        elif "finalized_at" in state:
            raise StateError(f"group {group_id} has a stray finalization time")

        shutdown = state.get("shutdown")
        if not isinstance(shutdown, dict):
            raise StateError(f"group {group_id} has invalid shutdown state")
        shutdown_fields = {
            "policy",
            "delay_minutes",
            "allow_remote",
            "allow_multi_user",
            "attempt_id",
            "attempted_at",
            "detail",
            "argv",
            "finished_at",
        }
        unknown_shutdown = set(shutdown) - shutdown_fields
        if unknown_shutdown:
            raise StateError(
                f"group {group_id} has unknown shutdown fields: "
                f"{sorted(unknown_shutdown)}"
            )
        policy = shutdown.get("policy")
        if policy not in SHUTDOWN_POLICIES:
            raise StateError(f"group {group_id} has invalid shutdown policy")
        delay = shutdown.get("delay_minutes")
        if not _is_plain_int(delay) or not 1 <= delay <= 60:
            raise StateError(f"group {group_id} has invalid shutdown delay")
        if not isinstance(shutdown.get("allow_remote"), bool) or not isinstance(
            shutdown.get("allow_multi_user"), bool
        ):
            raise StateError(f"group {group_id} has invalid shutdown overrides")
        detail = shutdown.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 4000):
            raise StateError(f"group {group_id} has invalid shutdown detail")

        attempt_id = shutdown.get("attempt_id")
        attempted_at = shutdown.get("attempted_at")
        if status_value in POWER_ATTEMPT_STATUSES:
            if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.fullmatch(
                attempt_id
            ):
                raise StateError(f"group {group_id} has invalid power attempt id")
            if not _is_timestamp(attempted_at):
                raise StateError(f"group {group_id} has invalid power attempt time")
        elif attempt_id is not None or attempted_at is not None:
            raise StateError(f"group {group_id} has a stray power attempt")

        if status_value in {"armed", "power_failed", "arming_unknown"}:
            if not _is_timestamp(shutdown.get("finished_at")):
                raise StateError(f"group {group_id} has no power result time")
            argv = shutdown.get("argv")
            if (
                not isinstance(argv, list)
                or len(argv) > 16
                or any(not isinstance(item, str) or len(item) > 1000 for item in argv)
            ):
                raise StateError(f"group {group_id} has invalid power argv")
        elif "finished_at" in shutdown or "argv" in shutdown:
            raise StateError(f"group {group_id} has stray power result fields")

        members = state.get("members")
        if not isinstance(members, dict):
            raise StateError(f"group {group_id} has invalid members")
        member_fields = {
            "member_id",
            "name",
            "workspace",
            "pid",
            "pid_start_time",
            "joined_at",
            "heartbeat_at",
            "finished_at",
            "status",
            "summary",
            "evidence",
        }
        statuses: list[str] = []
        for member_id, member in members.items():
            if not isinstance(member_id, str) or not MEMBER_ID_RE.fullmatch(member_id):
                raise StateError(f"group {group_id} has an invalid member id")
            if not isinstance(member, dict) or set(member) != member_fields:
                raise StateError(
                    f"group {group_id} member {member_id} has invalid fields"
                )
            if member.get("member_id") != member_id:
                raise StateError(f"group {group_id} member id mismatch")
            member_name = member.get("name")
            if (
                not isinstance(member_name, str)
                or not member_name.strip()
                or len(member_name) > 120
                or any(ord(ch) < 32 for ch in member_name)
            ):
                raise StateError(
                    f"group {group_id} member {member_id} has invalid name"
                )
            workspace = member.get("workspace")
            if not isinstance(workspace, str) or not workspace or "\x00" in workspace:
                raise StateError(
                    f"group {group_id} member {member_id} has invalid workspace"
                )
            try:
                workspace_is_absolute = Path(workspace).is_absolute()
            except (TypeError, ValueError):
                workspace_is_absolute = False
            if not workspace_is_absolute:
                raise StateError(
                    f"group {group_id} member {member_id} workspace is not absolute"
                )
            pid = member.get("pid")
            if not _is_plain_int(pid) or pid <= 0:
                raise StateError(f"group {group_id} member {member_id} has invalid pid")
            pid_start = member.get("pid_start_time")
            if pid_start is not None and (
                not isinstance(pid_start, str) or not pid_start.isdigit()
            ):
                raise StateError(
                    f"group {group_id} member {member_id} has invalid process identity"
                )
            if not _is_timestamp(member.get("joined_at")) or not _is_timestamp(
                member.get("heartbeat_at")
            ):
                raise StateError(
                    f"group {group_id} member {member_id} has invalid timestamps"
                )
            member_status = member.get("status")
            if member_status not in ({"active"} | TERMINAL_MEMBER_STATUSES):
                raise StateError(
                    f"group {group_id} member {member_id} has invalid status"
                )
            summary = member.get("summary")
            evidence = member.get("evidence")
            if (
                not isinstance(evidence, list)
                or len(evidence) > 100
                or any(
                    not isinstance(item, str) or len(item) > 1000 for item in evidence
                )
            ):
                raise StateError(
                    f"group {group_id} member {member_id} has invalid evidence"
                )
            if member_status == "active":
                if (
                    member.get("finished_at") is not None
                    or summary is not None
                    or evidence
                ):
                    raise StateError(
                        f"active member {member_id} in group {group_id} is finalized"
                    )
            elif (
                not _is_timestamp(member.get("finished_at"))
                or not isinstance(summary, str)
                or len(summary) > 4000
            ):
                raise StateError(
                    f"terminal member {member_id} in group {group_id} is incomplete"
                )
            statuses.append(member_status)

        member_count = len(members)
        if expected is not None:
            if member_count > expected or (sealed and member_count != expected):
                raise StateError(f"group {group_id} violates expected membership")
            if not sealed and member_count == expected:
                raise StateError(f"group {group_id} should already be sealed")
        if sealed and not members:
            raise StateError(f"sealed group {group_id} has no members")

        all_terminal = bool(statuses) and all(
            value in TERMINAL_MEMBER_STATUSES for value in statuses
        )
        if status_value in finalized_statuses and not all_terminal:
            raise StateError(f"finalized group {group_id} has nonterminal members")
        if status_value == "sealed" and all_terminal:
            raise StateError(f"sealed group {group_id} should already be finalized")

        eligible = False
        if policy == "completed":
            eligible = all_terminal and all(value == "completed" for value in statuses)
        elif policy == "finalized":
            eligible = all_terminal and all(
                value in FINALIZED_POWER_STATUSES for value in statuses
            )
        if status_value == "complete" and policy != "never":
            raise StateError(f"group {group_id} completed under a power policy")
        if status_value == "ineligible" and (policy == "never" or eligible):
            raise StateError(f"group {group_id} has inconsistent ineligible state")
        if status_value in POWER_ATTEMPT_STATUSES and not eligible:
            raise StateError(f"group {group_id} has an ineligible power attempt")

    def _save_locked(self, group_id: str, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        self._validate_state(state, group_id)
        atomic_write_json(self._state_path(group_id), state)

    def join(
        self,
        group_id: str,
        *,
        name: str,
        workspace: Path,
        pid: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        process_id = pid or os.getpid()
        if process_id <= 0:
            raise StateError("member pid must be positive")
        display_name = name.strip()
        if (
            not display_name
            or len(display_name) > 120
            or any(ord(ch) < 32 for ch in display_name)
        ):
            raise StateError("member name must be 1-120 printable characters")
        canonical_workspace = workspace.expanduser().resolve(strict=True)
        if not canonical_workspace.is_dir():
            raise StateError(f"workspace is not a directory: {canonical_workspace}")
        if self.root == canonical_workspace or self.root.is_relative_to(
            canonical_workspace
        ):
            raise StateError(
                "authoritative ciduxx state must be outside the registered workspace"
            )

        with self._lock(group_id):
            state = self._read_locked(group_id)
            if state["sealed"] or state["status"] != "open":
                raise StateError(f"group {group_id} is closed to new members")
            expected = state["expected_members"]
            if expected is not None and len(state["members"]) >= expected:
                raise StateError(f"group {group_id} already has its expected members")
            member_id = f"m-{uuid.uuid4().hex[:12]}"
            now = utc_now()
            state["members"][member_id] = {
                "member_id": member_id,
                "name": display_name,
                "workspace": str(canonical_workspace),
                "pid": process_id,
                "pid_start_time": process_start_time(process_id),
                "joined_at": now,
                "heartbeat_at": now,
                "finished_at": None,
                "status": "active",
                "summary": None,
                "evidence": [],
            }
            if expected is not None and len(state["members"]) == expected:
                state["sealed"] = True
                state["sealed_at"] = now
                state["status"] = "sealed"
            self._save_locked(group_id, state)
            return state, member_id

    def heartbeat(self, group_id: str, member_id: str) -> dict[str, Any]:
        _validate_component(member_id, MEMBER_ID_RE, "member id")
        with self._lock(group_id):
            state = self._read_locked(group_id)
            if state["status"] not in {"open", "sealed"}:
                raise StateError(
                    f"group {group_id} is {state['status']}; heartbeat is no longer accepted"
                )
            member = state["members"].get(member_id)
            if member is None:
                raise StateError(f"unknown member {member_id} in group {group_id}")
            if member["status"] != "active":
                raise StateError(f"member {member_id} is already {member['status']}")
            member["heartbeat_at"] = utc_now()
            self._save_locked(group_id, state)
            return state

    def seal(self, group_id: str) -> tuple[dict[str, Any], PowerIntent | None]:
        with self._lock(group_id):
            state = self._read_locked(group_id)
            if state["status"] in GROUP_FINAL_STATES or state["status"] in {
                "arming",
                "armed",
            }:
                raise StateError(f"group {group_id} is already finalized")
            if not state["members"]:
                raise StateError("cannot seal an empty group")
            expected = state["expected_members"]
            if expected is not None and len(state["members"]) != expected:
                raise StateError(
                    f"cannot seal: expected {expected} members, found {len(state['members'])}"
                )
            state["sealed"] = True
            state["sealed_at"] = state.get("sealed_at") or utc_now()
            state["status"] = "sealed"
            intent = self._prepare_power_locked(state)
            self._save_locked(group_id, state)
            return state, intent

    def finish(
        self,
        group_id: str,
        member_id: str,
        *,
        status_value: str,
        summary: str,
        evidence: Sequence[str] = (),
    ) -> tuple[dict[str, Any], PowerIntent | None]:
        _validate_component(member_id, MEMBER_ID_RE, "member id")
        if status_value not in TERMINAL_MEMBER_STATUSES:
            raise StateError(f"invalid terminal member status: {status_value}")
        if len(summary) > 4000:
            raise StateError("member summary exceeds 4000 characters")
        clean_evidence = [str(item)[:1000] for item in evidence[:100]]

        with self._lock(group_id):
            state = self._read_locked(group_id)
            member = state["members"].get(member_id)
            if member is None:
                raise StateError(f"unknown member {member_id} in group {group_id}")
            if member["status"] in TERMINAL_MEMBER_STATUSES:
                if member["status"] != status_value:
                    raise StateError(
                        f"member {member_id} is already terminal as {member['status']}"
                    )
                return state, None
            if state["status"] not in {"open", "sealed"}:
                raise StateError(
                    f"group {group_id} is {state['status']}; active members cannot finalize"
                )
            if member["status"] != "active":
                raise StateError(
                    f"member {member_id} cannot transition from {member['status']}"
                )
            member["status"] = status_value
            member["summary"] = summary
            member["evidence"] = clean_evidence
            member["finished_at"] = utc_now()
            member["heartbeat_at"] = utc_now()
            intent = self._prepare_power_locked(state)
            self._save_locked(group_id, state)
            return state, intent

    def cancel(self, group_id: str, reason: str) -> dict[str, Any]:
        with self._lock(group_id):
            state = self._read_locked(group_id)
            if state["status"] in {"arming", "armed", "arming_unknown"}:
                raise StateError(
                    "power may already be scheduled; inspect it and use shutdown -c manually"
                )
            if state["status"] in GROUP_FINAL_STATES:
                return state
            state["status"] = "cancelled"
            state["cancelled_at"] = utc_now()
            state["cancel_reason"] = reason[:1000]
            self._save_locked(group_id, state)
            return state

    def _prepare_power_locked(self, state: dict[str, Any]) -> PowerIntent | None:
        if state["status"] != "sealed" or not state["sealed"] or not state["members"]:
            return None
        expected = state["expected_members"]
        if expected is not None and len(state["members"]) != expected:
            return None
        statuses = [member["status"] for member in state["members"].values()]
        if not all(value in TERMINAL_MEMBER_STATUSES for value in statuses):
            return None

        policy = state["shutdown"]["policy"]
        eligible = False
        if policy == "completed":
            eligible = all(value == "completed" for value in statuses)
        elif policy == "finalized":
            eligible = all(value in FINALIZED_POWER_STATUSES for value in statuses)

        state["finalized_at"] = utc_now()
        if policy == "never":
            state["status"] = "complete"
            return None
        if not eligible:
            state["status"] = "ineligible"
            state["shutdown"]["detail"] = (
                f"terminal member statuses are not eligible for policy {policy}: {statuses}"
            )
            return None

        attempt_id = uuid.uuid4().hex
        state["status"] = "arming"
        state["shutdown"]["attempt_id"] = attempt_id
        state["shutdown"]["attempted_at"] = utc_now()
        state["shutdown"]["detail"] = "power scheduling attempt prepared"
        return PowerIntent(
            group_id=state["group_id"],
            attempt_id=attempt_id,
            delay_minutes=int(state["shutdown"]["delay_minutes"]),
            reason=f"ciduxx: all registered sessions finalized ({state['name']})",
            allow_remote=bool(state["shutdown"]["allow_remote"]),
            allow_multi_user=bool(state["shutdown"]["allow_multi_user"]),
        )

    @contextmanager
    def confirmed_power_intent(self, intent: PowerIntent) -> Iterator[None]:
        """Hold the group lock while confirming the exact persisted power intent."""

        with self._lock(intent.group_id):
            state = self._read_locked(intent.group_id)
            shutdown = state["shutdown"]
            expected_reason = (
                f"ciduxx: all registered sessions finalized ({state['name']})"
            )
            if state["status"] != "arming":
                raise StateError(
                    f"group {intent.group_id} is {state['status']}, not arming"
                )
            if shutdown["attempt_id"] != intent.attempt_id:
                raise StateError("power attempt id changed before adapter invocation")
            if (
                shutdown["delay_minutes"] != intent.delay_minutes
                or shutdown["allow_remote"] is not intent.allow_remote
                or shutdown["allow_multi_user"] is not intent.allow_multi_user
                or expected_reason != intent.reason
            ):
                raise StateError(
                    "persisted power intent changed before adapter invocation"
                )
            yield

    def complete_power(
        self, intent: PowerIntent, result: PowerResult
    ) -> dict[str, Any]:
        with self._lock(intent.group_id):
            state = self._read_locked(intent.group_id)
            if state["status"] != "arming":
                raise StateError(
                    f"group {intent.group_id} is {state['status']}, not in arming state"
                )
            if state["shutdown"]["attempt_id"] != intent.attempt_id:
                raise StateError("power attempt id mismatch")
            state["shutdown"]["detail"] = result.detail[:4000]
            state["shutdown"]["argv"] = list(result.argv)
            state["shutdown"]["finished_at"] = utc_now()
            if result.uncertain:
                state["status"] = "arming_unknown"
            elif result.success:
                state["status"] = "armed"
            else:
                state["status"] = "power_failed"
            self._save_locked(intent.group_id, state)
            return state


class SystemShutdownBackend:
    """One-shot Linux systemd shutdown adapter. It never invokes sudo or a shell."""

    ALLOWED_INPUTS = (Path("/usr/bin/shutdown"), Path("/sbin/shutdown"))
    ALLOWED_RESOLVED = {
        Path("/usr/bin/shutdown"),
        Path("/sbin/shutdown"),
        Path("/usr/bin/systemctl"),
        Path("/bin/systemctl"),
    }
    CONTAINER_MARKERS = (
        Path("/.dockerenv"),
        Path("/run/.containerenv"),
        Path("/run/systemd/container"),
    )
    VIRT_DETECTORS = (
        Path("/usr/bin/systemd-detect-virt"),
        Path("/bin/systemd-detect-virt"),
    )

    def __init__(
        self,
        *,
        allow_remote: bool = False,
        allow_multi_user: bool = False,
        timeout_seconds: int = 15,
    ) -> None:
        self.allow_remote = allow_remote
        self.allow_multi_user = allow_multi_user
        self.timeout_seconds = timeout_seconds

    def preflight(self) -> Path:
        if sys.platform != "linux":
            raise UnsafeEnvironment("automatic shutdown is Linux-only")
        if os.geteuid() == 0:
            raise UnsafeEnvironment(
                "refusing automatic shutdown from a root-run ciduxx process"
            )
        if self._inside_container():
            raise UnsafeEnvironment("refusing automatic shutdown inside a container")
        if os.environ.get("CI"):
            raise UnsafeEnvironment("refusing automatic shutdown in CI")
        kernel_text = ""
        for candidate in (Path("/proc/version"), Path("/proc/sys/kernel/osrelease")):
            try:
                kernel_text += candidate.read_text(encoding="utf-8").lower()
            except OSError:
                pass
        if "microsoft" in kernel_text or "wsl" in kernel_text:
            raise UnsafeEnvironment("refusing automatic shutdown inside WSL")
        try:
            pid_one = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UnsafeEnvironment("cannot inspect Linux PID 1") from exc
        if pid_one != "systemd" or not Path("/run/systemd/system").is_dir():
            raise UnsafeEnvironment(
                "automatic shutdown requires a running systemd host"
            )
        if not self.allow_remote and any(
            os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
        ):
            raise UnsafeEnvironment("refusing shutdown from a remote SSH session")
        users, remote_present, current_active = self._session_facts()
        if not current_active:
            raise UnsafeEnvironment(
                "no active logind session for the current user was proven"
            )
        if remote_present and not self.allow_remote:
            raise UnsafeEnvironment("a remote logind user session is active")
        if len(users) > 1 and not self.allow_multi_user:
            raise UnsafeEnvironment(
                f"refusing shutdown while multiple users are logged in: {', '.join(users)}"
            )
        return self._shutdown_binary()

    def _inside_container(self) -> bool:
        if any(path.exists() for path in self.CONTAINER_MARKERS):
            return True
        if os.environ.get("container"):
            return True
        detector = self._trusted_binary(self.VIRT_DETECTORS, "virtualization detector")
        try:
            result = subprocess.run(
                [str(detector), "--container", "--quiet"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise UnsafeEnvironment("timed out while detecting containers") from exc
        except OSError as exc:
            raise UnsafeEnvironment(f"cannot detect container state: {exc}") from exc
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        detail = (result.stdout + "\n" + result.stderr).strip()
        raise UnsafeEnvironment(
            detail or f"cannot establish container state (exit {result.returncode})"
        )

    def _session_facts(self) -> tuple[list[str], bool, bool]:
        loginctl = self._trusted_binary(
            (Path("/usr/bin/loginctl"), Path("/bin/loginctl")), "loginctl"
        )
        try:
            result = subprocess.run(
                [str(loginctl), "list-sessions", "--no-legend", "--no-pager"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UnsafeEnvironment("timed out while querying logind sessions") from exc
        except OSError as exc:
            raise UnsafeEnvironment(f"cannot query logind sessions: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stdout + "\n" + result.stderr).strip()
            raise UnsafeEnvironment(detail or "loginctl list-sessions failed")
        session_ids = [
            line.split()[0] for line in result.stdout.splitlines() if line.split()
        ]
        if not session_ids:
            raise UnsafeEnvironment("logind reported no sessions")

        users: set[str] = set()
        remote_present = False
        current_active = False
        current_name = pwd.getpwuid(os.geteuid()).pw_name
        for session_id in session_ids:
            try:
                detail_result = subprocess.run(
                    [
                        str(loginctl),
                        "show-session",
                        session_id,
                        "-p",
                        "Name",
                        "-p",
                        "Remote",
                        "-p",
                        "Active",
                        "-p",
                        "State",
                        "-p",
                        "Class",
                        "--no-pager",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise UnsafeEnvironment(
                    f"timed out while inspecting logind session {session_id}"
                ) from exc
            except OSError as exc:
                raise UnsafeEnvironment(
                    f"cannot inspect logind session {session_id}: {exc}"
                ) from exc
            if detail_result.returncode != 0:
                detail = (detail_result.stdout + "\n" + detail_result.stderr).strip()
                raise UnsafeEnvironment(
                    detail or f"loginctl show-session {session_id} failed"
                )
            properties: dict[str, str] = {}
            for line in detail_result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    properties[key] = value
            session_class = properties.get("Class", "")
            if not session_class.startswith("user"):
                continue
            name = properties.get("Name", "")
            if not name:
                raise UnsafeEnvironment(f"logind session {session_id} has no user name")
            users.add(name)
            is_remote = properties.get("Remote") == "yes"
            is_active = properties.get("Active") == "yes" or properties.get(
                "State"
            ) in {"active", "online"}
            remote_present = remote_present or is_remote
            if (
                name == current_name
                and is_active
                and (self.allow_remote or not is_remote)
            ):
                current_active = True
        if not users:
            raise UnsafeEnvironment("logind reported no user-class sessions")
        return sorted(users), remote_present, current_active

    def _trusted_binary(self, candidates: Sequence[Path], label: str) -> Path:
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                info = resolved.stat()
            except OSError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
                or not os.access(resolved, os.X_OK)
            ):
                continue
            return candidate
        raise UnsafeEnvironment(f"no trusted {label} binary was found")

    def _shutdown_binary(self) -> Path:
        for candidate in self.ALLOWED_INPUTS:
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                info = resolved.stat()
            except OSError:
                continue
            if resolved not in self.ALLOWED_RESOLVED:
                continue
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or not stat.S_ISREG(info.st_mode)
            ):
                continue
            return candidate
        raise UnsafeEnvironment("no trusted system shutdown binary was found")

    def schedule(self, delay_minutes: int, reason: str) -> PowerResult:
        if not isinstance(delay_minutes, int) or not 1 <= delay_minutes <= 60:
            raise PowerError("shutdown delay must be an integer from 1 to 60")
        clean_reason = " ".join(reason.split())[:120]
        if not clean_reason:
            raise PowerError("shutdown reason cannot be empty")
        binary = self.preflight()
        reservation = self._existing_reservation(binary)
        if reservation is not None:
            return PowerResult(
                False,
                f"an existing system shutdown reservation was found; not replaced: {reservation}",
                (str(binary), "--show"),
            )
        argv = (str(binary), "-P", f"+{delay_minutes}", clean_reason)
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
            )
        except subprocess.TimeoutExpired:
            return PowerResult(
                False,
                "shutdown command timed out; scheduling outcome is unknown; not retried",
                argv,
                uncertain=True,
            )
        except OSError as exc:
            return PowerResult(False, f"shutdown command could not start: {exc}", argv)
        detail = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            return PowerResult(True, detail or "shutdown accepted", argv)
        return PowerResult(
            False,
            detail or f"shutdown exited with status {result.returncode}",
            argv,
        )

    def _existing_reservation(self, binary: Path) -> str | None:
        argv = (str(binary), "--show")
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise PowerError(
                "timed out while checking existing shutdown reservations"
            ) from exc
        except OSError as exc:
            raise PowerError(
                f"cannot inspect existing shutdown reservations: {exc}"
            ) from exc
        detail = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            return detail or "scheduled shutdown with unknown details"
        if result.returncode == 1 and "no scheduled shutdown" in detail.lower():
            return None
        raise PowerError(
            detail
            or f"cannot establish shutdown reservation state (exit {result.returncode})"
        )


def execute_power_intent(
    store: GroupStore,
    intent: PowerIntent | None,
    backend: Any | None = None,
    *,
    allow_real_power: bool = False,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if backend is None:
        if allow_real_power is not True:
            return store.complete_power(
                intent,
                PowerResult(
                    False,
                    "real power backend is disabled without explicit allow_real_power",
                ),
            )
        try:
            store.assert_real_power_ready()
        except CiduxxError as exc:
            return store.complete_power(intent, PowerResult(False, str(exc)))
        power_backend: Any = SystemShutdownBackend(
            allow_remote=intent.allow_remote,
            allow_multi_user=intent.allow_multi_user,
        )
    else:
        power_backend = backend

    with store.power_lock():
        ledger_path = store.root / "power.json"
        if ledger_path.exists() or ledger_path.is_symlink():
            try:
                existing_ledger = load_json(ledger_path)
                _validate_power_ledger(existing_ledger)
            except StateError as exc:
                return store.complete_power(
                    intent,
                    PowerResult(
                        False,
                        f"existing power ledger is invalid; refusing power: {exc}",
                    ),
                )
            if existing_ledger["boot_id"] == boot_id():
                return store.complete_power(
                    intent,
                    PowerResult(
                        False,
                        "another ciduxx power attempt already exists for this Linux boot; not retried",
                    ),
                )
        ledger = {
            "schema_version": SCHEMA_VERSION,
            "boot_id": boot_id(),
            "owner_uid": os.geteuid(),
            "group_id": intent.group_id,
            "attempt_id": intent.attempt_id,
            "status": "arming",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "detail": "power adapter call prepared",
        }
        atomic_write_json(ledger_path, ledger)
        with store.confirmed_power_intent(intent):
            try:
                result = power_backend.schedule(intent.delay_minutes, intent.reason)
            except CiduxxError as exc:
                result = PowerResult(False, str(exc))
            except Exception as exc:
                result = PowerResult(
                    False,
                    f"unexpected power adapter failure; outcome is unknown: {exc}",
                    uncertain=True,
                )
        if (
            not isinstance(result, PowerResult)
            or not isinstance(result.success, bool)
            or not isinstance(result.uncertain, bool)
            or not isinstance(result.detail, str)
            or not isinstance(result.argv, tuple)
            or any(not isinstance(item, str) for item in result.argv)
            or (result.success and result.uncertain)
        ):
            result = PowerResult(
                False,
                "power adapter returned an invalid result; outcome is unknown",
                uncertain=True,
            )
        if result.uncertain:
            ledger["status"] = "arming_unknown"
        elif result.success:
            ledger["status"] = "armed"
        else:
            ledger["status"] = "power_failed"
        ledger["updated_at"] = utc_now()
        ledger["detail"] = result.detail[:4000]
        ledger["argv"] = list(result.argv)
        atomic_write_json(ledger_path, ledger)
        return store.complete_power(intent, result)


def secure_artifact_dir(workspace: Path, run_id: str) -> Path:
    """Create the fixed workspace report path while rejecting symlink components."""

    if not re.fullmatch(r"r-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id):
        raise StateError(f"invalid run id: {run_id!r}")
    root = workspace.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise StateError(f"workspace is not a directory: {root}")
    current = root
    for component in (".ciduxx", "runs", run_id):
        current = current / component
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise StateError(f"unsafe artifact path component: {current}")
            if info.st_uid != os.geteuid():
                raise StateError(f"artifact path has unexpected owner: {current}")
        else:
            current.mkdir(mode=0o700)
    return current


def clean_markdown(value: Any, limit: int = 4000) -> str:
    text = str(value).replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return text[:limit]


def render_decisions(decisions: Sequence[Mapping[str, Any]], iteration: int) -> str:
    blocks: list[str] = []
    for index, decision in enumerate(decisions, start=1):
        decision_id = f"D-{iteration:03d}-{index:02d}"
        question = clean_markdown(
            decision.get("question", "Unspecified decision"), 1000
        )
        status_value = clean_markdown(decision.get("status", "DEFERRED"), 40).upper()
        options = decision.get("options", [])
        lines = [
            f"## {decision_id} - {question}",
            "",
            f"Status: {status_value}",
            f"Iteration: {iteration}",
            f"Question: {question}",
            "",
        ]
        if isinstance(options, list):
            for option_index, option in enumerate(options[:26]):
                if not isinstance(option, Mapping):
                    continue
                label = clean_markdown(option.get("label") or chr(65 + option_index), 8)
                label = re.sub(r"[^A-Za-z0-9_-]", "", label) or chr(65 + option_index)
                text = clean_markdown(option.get("text", ""), 1000)
                lines.append(f"{label}: {text}")
        lines.extend(
            [
                "",
                f"Chosen: {clean_markdown(decision.get('chosen') or 'NONE', 40)}",
                f"Basis: {clean_markdown(decision.get('basis', ''), 2000)}",
                f"Action: {clean_markdown(decision.get('action', ''), 2000)}",
                f"Rollback: {clean_markdown(decision.get('rollback', ''), 2000)}",
                f"Revisit when: {clean_markdown(decision.get('revisit_when', ''), 2000)}",
            ]
        )
        evidence = decision.get("evidence", [])
        if isinstance(evidence, list) and evidence:
            lines.append(
                "Evidence: "
                + "; ".join(clean_markdown(item, 500) for item in evidence[:20])
            )
        blocks.append("\n".join(lines).rstrip())
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def workspace_fingerprint(workspace: Path) -> str:
    digest = hashlib.sha256()
    commands = (
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
        ["git", "diff", "--stat", "--no-ext-diff"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            digest.update(str(result.returncode).encode())
            digest.update(result.stdout[:4_000_000])
            digest.update(result.stderr[:100_000])
        except (OSError, subprocess.TimeoutExpired) as exc:
            digest.update(repr(exc).encode())
    return digest.hexdigest()


def new_run_id() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return f"r-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
