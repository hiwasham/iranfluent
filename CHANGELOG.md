# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to a four-part `W.X.Y.Z` version scheme.

## [1.0.0.0] - 2026-09-28

### Added

- Guarded, add-only FluentCRM tag operator: preview a tag change, confirm it, then execute it, with every step recorded. It only ever adds one pre-approved tag (`set_vocab_B1`) and never removes or edits existing tags.
- Human-confirmation boundary: nothing is written to FluentCRM until you approve the previewed change. Previews expire, and a cancelled or expired preview can never be executed.
- Fail-closed safety throughout: the operator refuses to start if its startup checks fail, refuses to act on an expired safety approval, and stops rather than guessing whenever a remote response is missing, malformed, or identifies the wrong contact.
- Post-write verification: after a tag is added, the operator re-reads the contact (with retries for read-replica lag) to confirm the change landed, and reports `confirmed`, `settled`, or `ambiguous` instead of assuming success.
- Reconcile command: recover a clean, accurate state after any interruption or ambiguous result without ever re-sending a write.
- Append-only audit trail in a local SQLite store, with contact emails stored only as HMAC-SHA-256 hashes, so every preview, execution, and rejection is recorded without keeping raw personal data.
- Operator runbook and an Infisical-backed launcher that supplies credentials to the operator as a child process only, so secrets never land in files, shell history, or the transcript.
