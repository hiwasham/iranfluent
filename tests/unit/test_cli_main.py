"""In-process tests for ``cli.main`` orchestration and its impure seams.

Every ``main`` branch (fail-closed startup, happy dispatch, parse/factory
rejection with best-effort audit, dispatch CommandError vs unexpected error)
and every helper (``_run_git``, ``_build_application``, ``_dispatch``,
``_audit_cli_rejection``) is driven here with injected seams so the 100% branch
gate holds without measuring subprocess execution.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iranfluent_tag_operator import cli
from iranfluent_tag_operator.application import Application
from iranfluent_tag_operator.config import (
    ENV_APP_PASSWORD,
    ENV_AUDIT_HMAC_KEY,
    ENV_BASE_URL,
    ENV_USERNAME,
    ContractFixture,
    Credentials,
    RuntimeIdentity,
)
from iranfluent_tag_operator.models import (
    CancelCommand,
    CommandError,
    ErrorCategory,
    ExecuteCommand,
    PreviewCommand,
    ReconcileCommand,
    TagDefinition,
)
from iranfluent_tag_operator.state import StateStore

UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
IDENT = RuntimeIdentity(
    package_version="0.1.0", git_commit="a" * 40,
    uv_lock_sha256="b" * 64, allowlist_sha256="c" * 64,
)
TAG = TagDefinition(
    key="vocab_b1", id=269, title="set_vocab_B1", slug="set_vocab_b1",
    purpose="B1 vocabulary cohort", risk_note="none",
    business_reviewed_at="2026-07-30T00:00:00Z",
    business_review_expires_at="2026-08-29T00:00:00Z",
    business_reviewer="ops", known_downstream_effects=(), enabled=True,
)
CREDS = Credentials(base_url="https://x", username="u", app_password="p w",
                    audit_hmac_key="k")
GOOD_ENV = {
    ENV_BASE_URL: "https://x", ENV_USERNAME: "u",
    ENV_APP_PASSWORD: "p w", ENV_AUDIT_HMAC_KEY: "k",
}
PREVIEW_JSON = (b'{"command":"preview","operation":"add_tag",'
                b'"email":"a@b.co","tag_key":"vocab_b1"}')
# placeholder-body


def _clean_git(args, cwd):
    # rev-parse returns a full commit; status --porcelain returns a clean tree.
    return "a" * 40 + "\n" if args[0] == "rev-parse" else ""


class FakeResp:
    def to_json_obj(self):
        return {"ok": True, "request_id": "req-x"}


class FakeApp:
    """Stands in for Application at the dispatch seam; ``preview`` is the path
    driven by the injected preview command."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc

    def preview(self, command):
        if self._exc is not None:
            raise self._exc
        return FakeResp()


def _run_main(tmp_path: Path, *, raw=PREVIEW_JSON, environ=None, run_git=_clean_git,
              project_root=None, app_factory=None):
    stdin = io.BytesIO(raw)
    stdout, stderr = io.StringIO(), io.StringIO()
    kwargs = dict(
        stdin=stdin, stdout=stdout, stderr=stderr,
        environ=dict(GOOD_ENV if environ is None else environ),
        now=lambda: NOW, monotonic=lambda: 100.0, sleep=lambda _: None,
        new_request_id=lambda: "req-x", run_git=run_git,
        project_root=cli.PROJECT_ROOT if project_root is None else project_root,
        state_path=tmp_path / "state.sqlite3",
    )
    if app_factory is not None:
        kwargs["app_factory"] = app_factory
    code = cli.main(**kwargs)
    return stdout.getvalue(), stderr.getvalue(), code


def _audit_events(tmp_path: Path):
    s = StateStore.connect(identity=IDENT, actor="tool/operator",
                           path=tmp_path / "state.sqlite3")
    try:
        return [r[0] for r in s._conn.execute("SELECT event_type FROM audit ORDER BY seq")]
    finally:
        s.close()


# --- startup preflight branches -------------------------------------------

def test_startup_dirty_worktree_rejects(tmp_path):
    def dirty(args, cwd):
        return "a" * 40 + "\n" if args[0] == "rev-parse" else " M file\n"
    out, err, code = _run_main(tmp_path, run_git=dirty)
    assert code == 5
    assert json.loads(out)["error"]["category"] == "runtime_identity_invalid"
    assert "(request_id=-)" in err  # null request id renders as '-'


def test_startup_unexpected_exception_is_internal(tmp_path):
    def boom(args, cwd):
        raise KeyError("not in the caught tuple")
    out, err, code = _run_main(tmp_path, run_git=boom)
    assert code == 5
    assert json.loads(out)["error"]["category"] == "internal_error"


def test_gather_identity_missing_lockfile_rejects(tmp_path):
    # An empty project root has no uv.lock: the read OSError fails closed.
    out, err, code = _run_main(tmp_path, project_root=tmp_path)
    assert code == 5
    assert json.loads(out)["error"]["category"] == "runtime_identity_invalid"


# --- happy path and dispatch branches -------------------------------------

def test_preview_success_emits_one_object(tmp_path):
    out, err, code = _run_main(tmp_path, app_factory=lambda **kw: FakeApp())
    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {"ok": True, "request_id": "req-x"}
    assert err == ""


def test_contract_stale_via_default_factory_is_audited(tmp_path):
    # No committed contract fixture -> load_contract_file fails closed, and the
    # CLI-layer rejection is audited as preview_rejected with a null request id.
    out, err, code = _run_main(tmp_path)
    assert code == 2
    assert json.loads(out)["error"]["category"] == "contract_stale"
    assert _audit_events(tmp_path) == ["preview_rejected"]


def test_dispatch_command_error_renders_request_id(tmp_path):
    exc = CommandError(ErrorCategory.CONTACT_NOT_FOUND, request_id="req-x")
    out, err, code = _run_main(tmp_path, app_factory=lambda **kw: FakeApp(exc))
    assert code == 2
    assert json.loads(out)["request_id"] == "req-x"
    assert "(request_id=req-x)" in err
    # Already audited inside the application: the CLI adds no rejection row.
    assert _audit_events(tmp_path) == []


def test_dispatch_unexpected_exception_is_internal(tmp_path):
    out, err, code = _run_main(tmp_path, app_factory=lambda **kw: FakeApp(ValueError("x")))
    assert code == 5
    assert json.loads(out)["error"]["category"] == "internal_error"


# --- impure helper units ---------------------------------------------------

def test_run_git_success_and_failure():
    assert len(cli._run_git(["rev-parse", "HEAD"], cli.PROJECT_ROOT).strip()) in (40, 64)
    with pytest.raises(RuntimeError):
        cli._run_git(["rev-parse", "no-such-ref-zzz"], cli.PROJECT_ROOT)


def test_build_application_success(tmp_path):
    fixture = ContractFixture(
        schema_version=1, generated_at="2026-08-01T00:00:00+00:00",
        expires_at="2026-08-20T00:00:00+00:00", site="https://x",
        rest_namespace="fluent-crm/v2", plugin_version="3.1.5",
        endpoints={"contact_search": "/s", "tag_lookup": "/t",
                   "contact_detail": "/c", "attach_tags": "/a"},
        pagination_mode="page_number", contact_search_page_limit=50,
        tag_lookup_page_limit=50,
        response_keys={"contact_search": ("data",), "tag_lookup": ("data",)},
        mutation_method="POST", mutation_path="/a", mutation_body_keys=("tags",),
        success_status=200, success_body_keys=(), runtime_identity=IDENT,
    )
    store = StateStore.connect(identity=IDENT, actor="tool/operator",
                               path=tmp_path / "state.sqlite3")
    try:
        app = cli._build_application(
            credentials=CREDS, tag=TAG, store=store, now=lambda: NOW,
            monotonic=lambda: 100.0, sleep=lambda _: None,
            new_request_id=lambda: "req-x", load_contract=lambda: fixture,
        )
        assert isinstance(app, Application)
    finally:
        store.close()


def test_dispatch_routes_each_command():
    class RecApp:
        def preview(self, c): return "P"
        def execute(self, c): return "E"
        def cancel(self, c): return "C"
        def reconcile(self, c): return "R"
    app = RecApp()
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert cli._dispatch(app, PreviewCommand(email="a@b.co", tag_key="vocab_b1")) == "P"
    assert cli._dispatch(app, ExecuteCommand(request_id=uuid)) == "E"
    assert cli._dispatch(app, CancelCommand(request_id=uuid)) == "C"
    assert cli._dispatch(app, ReconcileCommand(request_id=uuid)) == "R"


def test_audit_cli_rejection_swallows_store_error():
    class RaisingStore:
        def record_rejection(self, *a):
            raise CommandError(ErrorCategory.LOCAL_STATE_BUSY)
    # Best-effort: a busy store must not mask the original rejection.
    cli._audit_cli_rejection(
        RaisingStore(), CommandError(ErrorCategory.INVALID_JSON), lambda: NOW,
    )


def test_default_clock_and_request_id_seams():
    # Production defaults are injected away in every other test; call them once.
    assert cli._utcnow().tzinfo is UTC
    assert cli._UUID_V4_RE.match(cli._new_request_id())
