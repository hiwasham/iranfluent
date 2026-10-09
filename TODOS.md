# TODOS

## FluentCRM Tag Operator

### Package the operator for user-level installation

**What:** Package the operator for installation outside a mutable repository checkout.

**Why:** Repository-only `uv run --locked` execution is acceptable for one operator but adds friction to repeated use and upgrades.

**Context:** Build this only after version-one usage proves the workflow valuable. Preserve the same package version, source commit, dependency lock, allowlist digest, and audit identity guarantees. Define release, upgrade, rollback, and artifact-integrity procedures before replacing clean-checkout execution.

**Effort:** M
**Priority:** P3
**Depends on:** Completed version one and observed operational usage

### Add individually reviewed FluentCRM tags

**What:** Add support for approved FluentCRM tags beyond `vocab_b1`.

**Why:** Future operator requests may need other vocabulary levels or course-access tags while retaining deterministic tag selection.

**Context:** Add tags individually after version one demonstrates demand. Each tag requires exact live identity verification, a documented review of downstream access, email, and automation effects, a maximum 30-day approval window, contract fixtures, and full tests. Do not introduce arbitrary IDs, fuzzy tag selection, or runtime allowlist overrides.

**Effort:** M per tag
**Priority:** P3
**Depends on:** Completed version one, demonstrated demand, and separate business-safety approval for each tag

## Completed
