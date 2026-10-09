"""Process entry point: strict stdin JSON parse, fail-closed startup, one JSON out.

``main`` reads at most 16 KiB from stdin, parses exactly one JSON object, runs the
fail-closed startup preflight (runtime identity, credentials, private state store,
write-lock probe), dispatches to :class:`~iranfluent_tag_operator.application.Application`,
and serializes exactly one JSON object to stdout for every path. Every process exit
code comes from :data:`~iranfluent_tag_operator.models.EXIT_CODES`; stderr carries only
a sanitized category plus request id, never a secret, response body, path, or raw
exception text. All impure seams (stdin/stdout/stderr, environment, clocks, git,
state path, application factory) are injected with production defaults so every
branch is exercised in-process; the console script calls ``main()`` with no arguments.

Startup preflight failures are UNAUDITABLE: no audit row can be written because the
store may be the thing that failed, so they emit a sanitized JSON error and exit
without touching FluentCRM. Once the store is open, every rejected attempt is audited;
CLI-layer parse and contract rejections are recorded as ``preview_rejected`` with a
null request id (design: malformed input cannot yield a trustworthy request id),
while application-layer rejections audit themselves inside the application.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import IO

from .application import Application
from .config import (
    ALLOWLIST_PATH,
    RuntimeIdentity,
    build_runtime_identity,
    check_contract_fresh,
    load_allowlist,
    load_contract_file,
    load_credentials,
)
from .fluentcrm import FluentCrmClient
from .models import (
    OPERATION_ADD_TAG,
    CancelCommand,
    CommandError,
    ErrorCategory,
    ExecuteCommand,
    PreviewCommand,
    ReconcileCommand,
)
from .state import AuditFields, StateStore

MAX_STDIN_BYTES = 16 * 1024
PACKAGE_NAME = "iranfluent-tag-operator"
ACTOR = "tool/operator"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GIT_TIMEOUT = 10.0

_COMMANDS = frozenset({"preview", "execute", "cancel", "reconcile"})
_PREVIEW_KEYS = frozenset({"command", "operation", "email", "tag_key"})
_ID_KEYS = frozenset({"command", "request_id"})
# Canonical lowercase hyphenated UUIDv4: version nibble 4, variant nibble 8/9/a/b.
_UUID_V4_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_request_id() -> str:
    return str(uuid.uuid4())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # A duplicate key makes the input not a single well-formed object; surface it
    # as a plain ValueError so the json.loads guard maps it to invalid_json.
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


# --- strict stdin parse: exactly one JSON object, one command --------------

def _read_stdin(stdin: IO[bytes]) -> bytes:
    # One byte past the cap lets an oversized payload be detected and rejected.
    return stdin.read(MAX_STDIN_BYTES + 1)


def parse_command(
    raw: bytes,
) -> PreviewCommand | ExecuteCommand | CancelCommand | ReconcileCommand:
    """Parse raw stdin bytes into exactly one command or raise ``CommandError``."""

    if not raw or len(raw) > MAX_STDIN_BYTES or raw[:3] == b"\xef\xbb\xbf":
        raise CommandError(ErrorCategory.INVALID_JSON)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CommandError(ErrorCategory.INVALID_JSON)
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        # Malformed JSON, trailing values, or a duplicate key: all invalid_json.
        raise CommandError(ErrorCategory.INVALID_JSON)
    if not isinstance(obj, dict):
        # Arrays, scalars, and null are not a command object.
        raise CommandError(ErrorCategory.INVALID_JSON)
    command = obj.get("command")
    if not isinstance(command, str) or command not in _COMMANDS:
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    if command == "preview":
        return _parse_preview(obj)
    return _parse_id_command(obj, command)


def _parse_preview(obj: dict[str, object]) -> PreviewCommand:
    if set(obj) != _PREVIEW_KEYS:
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    operation = obj["operation"]
    if not isinstance(operation, str) or operation != OPERATION_ADD_TAG:
        raise CommandError(ErrorCategory.UNSUPPORTED_OPERATION)
    tag_key = obj["tag_key"]
    if not isinstance(tag_key, str):
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    email = obj["email"]
    if not isinstance(email, str):
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    if len(email) > 254 or email.count("@") != 1:
        raise CommandError(ErrorCategory.INVALID_EMAIL)
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise CommandError(ErrorCategory.INVALID_EMAIL)
    # Allowlist membership and the live tag/status checks belong to the
    # application, which audits its own rejection; the parser stays structural.
    return PreviewCommand(email=email, tag_key=tag_key)


def _parse_id_command(
    obj: dict[str, object], command: str,
) -> ExecuteCommand | CancelCommand | ReconcileCommand:
    if set(obj) != _ID_KEYS:
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    request_id = obj["request_id"]
    if not isinstance(request_id, str) or not _UUID_V4_RE.match(request_id):
        raise CommandError(ErrorCategory.INVALID_COMMAND)
    if command == "execute":
        return ExecuteCommand(request_id=request_id)
    if command == "cancel":
        return CancelCommand(request_id=request_id)
    return ReconcileCommand(request_id=request_id)


# --- fail-closed startup preflight -----------------------------------------

def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError("git command failed")
    return result.stdout


def _gather_identity(
    project_root: Path, run_git: Callable[[list[str], Path], str],
) -> tuple[RuntimeIdentity, bytes]:
    """Shell to git and read the lockfile/allowlist, then delegate to the pure
    validator. Any impure failure fails closed as ``runtime_identity_invalid``;
    a dirty worktree or bad hash is rejected inside ``build_runtime_identity``."""

    try:
        commit = run_git(["rev-parse", "HEAD"], project_root).strip()
        status = run_git(["status", "--porcelain"], project_root)
        version = metadata.version(PACKAGE_NAME)
        uv_lock_bytes = (project_root / "uv.lock").read_bytes()
        allowlist_bytes = ALLOWLIST_PATH.read_bytes()
    except (OSError, subprocess.SubprocessError, RuntimeError, metadata.PackageNotFoundError):
        raise CommandError(ErrorCategory.RUNTIME_IDENTITY_INVALID)
    identity = build_runtime_identity(
        package_version=version, git_commit=commit, git_status_porcelain=status,
        uv_lock_bytes=uv_lock_bytes, allowlist_bytes=allowlist_bytes,
    )
    return identity, allowlist_bytes


def _build_application(
    *,
    credentials,
    tag,
    store: StateStore,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    new_request_id: Callable[[], str],
    load_contract: Callable[[], object] = load_contract_file,
) -> Application:
    """Load and freshness-check the reviewed contract, then wire the real client
    and application. A missing or expired fixture fails closed as ``contract_stale``.
    The client and application share the injected monotonic clock end to end."""

    contract = load_contract()
    check_contract_fresh(contract, now())
    client = FluentCrmClient(credentials, contract, monotonic=monotonic, sleep=sleep)
    return Application(
        client=client, store=store, tag_definition=tag,
        audit_hmac_key=credentials.audit_hmac_key, now=now,
        monotonic=monotonic, sleep=sleep, new_request_id=new_request_id,
    )


# --- dispatch, rendering, and CLI-layer audit ------------------------------

def _dispatch(app: Application, command: object) -> object:
    if isinstance(command, PreviewCommand):
        return app.preview(command)
    if isinstance(command, ExecuteCommand):
        return app.execute(command)
    if isinstance(command, CancelCommand):
        return app.cancel(command)
    return app.reconcile(command)


def _emit(obj: dict[str, object], stdout: IO[str]) -> None:
    stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")


def _render_error(exc: CommandError, stdout: IO[str], stderr: IO[str]) -> int:
    # Both streams carry only the safe category and request id: no secret, no
    # response body, no path, no raw exception text ever crosses this boundary.
    _emit(exc.to_json_obj(), stdout)
    stderr.write(f"{exc.category.value} (request_id={exc.request_id or '-'})\n")
    return exc.exit_code


def _audit_cli_rejection(
    store: StateStore, exc: CommandError, now: Callable[[], datetime],
) -> None:
    # A parse/contract rejection has no trustworthy request id; record it as
    # preview_rejected with a null id. Best-effort: a busy store must not mask
    # the original rejection with a durability error.
    try:
        store.record_rejection(
            "preview_rejected",
            AuditFields(error_category=exc.category.value, http_status=exc.http_status),
            now(),
        )
    except CommandError:
        pass


def main(
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    new_request_id: Callable[[], str] = _new_request_id,
    project_root: Path = PROJECT_ROOT,
    run_git: Callable[[list[str], Path], str] = _run_git,
    state_path: Path | None = None,
    app_factory: Callable[..., Application] = _build_application,
) -> int:
    """Run one operator command and return its process exit code."""

    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    environ = os.environ if environ is None else environ

    # Startup preflight: unauditable on failure (the store may be what failed),
    # so emit exactly one sanitized JSON object and never touch FluentCRM.
    try:
        identity, allowlist_bytes = _gather_identity(project_root, run_git)
        tag = load_allowlist(allowlist_bytes)
        credentials = load_credentials(environ)
        store = StateStore.connect(identity=identity, actor=ACTOR, path=state_path)
        store.probe_write_lock()
    except CommandError as exc:
        return _render_error(exc, stdout, stderr)
    except Exception:
        return _render_error(CommandError(ErrorCategory.INTERNAL_ERROR), stdout, stderr)

    try:
        store.run_maintenance(now())  # best-effort retention; never blocks work
        raw = _read_stdin(stdin)
        try:
            command = parse_command(raw)
            app = app_factory(
                credentials=credentials, tag=tag, store=store, now=now,
                monotonic=monotonic, sleep=sleep, new_request_id=new_request_id,
            )
        except CommandError as exc:
            _audit_cli_rejection(store, exc, now)
            return _render_error(exc, stdout, stderr)
        try:
            response = _dispatch(app, command)
        except CommandError as exc:
            return _render_error(exc, stdout, stderr)  # already audited by application
        except Exception:
            return _render_error(CommandError(ErrorCategory.INTERNAL_ERROR), stdout, stderr)
        _emit(response.to_json_obj(), stdout)
        return 0
    finally:
        store.close()



