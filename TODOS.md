# TODOS

## FluentCRM Tag Operator

### Package the operator for user-level installation

**What:** Package the operator for installation outside a mutable repository checkout.

**Why:** Repository-only `uv run --locked` execution is acceptable for one operator but adds friction to repeated use and upgrades.

**Context:** Build this only after version-one usage proves the workflow valuable. Preserve the same package version, source commit, dependency lock, allowlist digest, and audit identity guarantees. Define release, upgrade, rollback, and artifact-integrity procedures before replacing clean-checkout execution.

**Effort:** M
**Priority:** P3
**Depends on:** Completed version one and observed operational usage

## Completed
