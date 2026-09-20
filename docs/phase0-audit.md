# Phase 0 Final Audit

Self-audit against the 23 checks required before declaring Phase 0 complete. Each item is
answered honestly — "documented only" and "partially implemented" are recorded as such, not
rounded up to "done."

| # | Check | Status | Evidence |
|---|---|---|---|
| 1 | Every requirement has a documented location | ✅ | Every `docs/*.md` cross-references the ADR/requirement it implements; `docs/open-decisions.md` covers what's unresolved. |
| 2 | eDAR schema matches the source document | ✅ | `schemas/edar-schema.json` built field-by-field from `eDAR Fields.pdf`; verified by `csc_apps/edar/tests.py::EdarSchemaFileTests`. |
| 3 | Exactly 42 target fields | ✅ | `test_field_count_is_42_across_seven_modules` (module field counts sum to 42, verified programmatically, not just asserted in prose). |
| 4 | Seven modules preserved | ✅ | Same test — 7 modules, A–G. |
| 5 | Maximum of 3 vehicles enforced | ✅ | `schemas/edar-schema.json` module D `max_repetitions: 3`; `resolve_field_key()` raises `ValueError` past index 3, tested (`test_rejects_vehicle_index_beyond_max_repetitions`). |
| 6 | Casualty/person records repeatable | ✅ | Module E `max_repetitions: null`; `test_casualty_field_has_no_upper_index_bound` confirms no artificial cap. |
| 7 | GPS treated as device-captured data | ✅ | ADR-012; `Recording.gps_latitude/longitude/captured_at`, never written by extraction code, no `EdarFieldValue` row for GPS. |
| 8 | Translation is mandatory | ⚠️ Documented, not yet code-enforced | ADR-004 states the contract (`Transcript.language=ENGLISH` always derived from `ORIGINAL`); no pipeline code exists yet to enforce it at runtime since STT/translation providers aren't implemented (Phase 1). |
| 9 | Single-speaker processing assumed | ✅ | ADR-005; `Transcript` has no speaker/diarization field anywhere in the schema. |
| 10 | Live and uploaded audio both supported | ⚠️ Modeled, not yet exposed | `Audio.source` choices `LIVE`/`UPLOAD` exist; no upload/recording HTTP endpoint exists yet (`docs/api-architecture.md` §1 marks these "models only"). |
| 11 | Raw audio, AI output, and approved output separated | ✅ | `Audio` (Layer 1) vs. `EdarFieldValue.layer=AI` (Layer 2) vs. `layer=APPROVED` (Layer 3) are structurally distinct, `unique_together` per layer — ADR-008. |
| 12 | AI output is schema-validated | ⚠️ Validator implemented, not yet wired to a live pipeline | `csc_apps/edar/schema_validation.py::validate_extraction_entry` exists and is tested against the real schema file; no extraction provider exists yet to call it against real AI output (OD-002). |
| 13 | Every extracted field traceable to source evidence | ✅ | `EdarFieldValue.confidence/source_transcript_segment/source_start_time/source_end_time/extraction_version` — ADR-010. |
| 14 | Long-running operations are asynchronous | ⚠️ Interface only | `TaskRunner`/`InlineTaskRunner` exist and are tested; `InlineTaskRunner` is explicitly dev-only synchronous execution — the production async implementation is OD-003, deliberately deferred. |
| 15 | Retryable vs. non-retryable failures distinguished | ✅ | `csc_apps/processing/error_classification.py`, tested including the "unrecognized code raises" case. |
| 16 | Authentication separated from authorization | ⚠️ Authentication done; authorization not yet wired to endpoints | `JWTAuthentication` + `User.role` implemented and tested; no protected domain endpoint exists yet to apply a role check to (only `/auth/login/`, which is intentionally unauthenticated) — `docs/security-baseline.md` §2 defines the intended rules. |
| 17 | Audio files private | ⚠️ Requirement documented; no storage/serving code exists yet | `docs/security-baseline.md` §3 states the requirement and rejects PMS's public-media-serving pattern; `Audio.storage_path` is an opaque, backend-agnostic key so the eventual private-storage implementation (OD-008) doesn't require a domain-model change. |
| 18 | Secrets protected | ✅ | `.env` git-ignored, `.env.example` has variable names only, `SECRET_KEY`/DB credentials read via `python-decouple`, never hardcoded. |
| 19 | Implementation follows PMS conventions | ✅ | `docs/pms-reference-analysis.md` documents every convention with evidence; code review of `csc_apps/common`, `csc_apps/authentication` confirms controller→view→model layering, response envelope, `NOT_PROVIDED` sentinel, and exception funnel match. |
| 20 | No unnecessary dependencies | ✅ | `requirements.txt` (11 packages) is a strict subset of PMS's own `requirements.txt` — nothing added that PMS doesn't already depend on. |
| 21 | Unsupported assumptions clearly marked | ✅ | SOURCE REQUIREMENT / ARCHITECTURAL DECISION / ASSUMPTION / OPEN DECISION labels used throughout `docs/`, e.g. `docs/product-scope.md` §5 on the Reviewer role. |
| 22 | Open architectural decisions explicitly documented | ✅ | `docs/open-decisions.md`, 9 items (OD-001 through OD-009), each with an interim default and a recommendation. |
| 23 | Can Phase 1 begin without redesigning Phase 0 | ✅ (assessed) | Every Phase 1 addition (concrete providers, a production `TaskRunner`, domain CRUD endpoints, audio storage backend) implements an existing interface or adds new `EdarFieldValue`/`ProcessingEvent` rows — none require changing `schemas/edar-schema.json`'s shape, the state machine, or the three-layer model. |

## What this audit does not claim

Six items above (#8, #10, #12, #14, #16, #17) are marked ⚠️ rather than ✅: they are
correctly *architected* (an interface, a contract, a documented
rule) but not yet *exercised end-to-end*, because doing so requires components Phase 0
explicitly does not build (a real AI provider, a production task queue, upload/review HTTP
endpoints, a chosen storage backend). This is the intended Phase 0/Phase 1 boundary
(`docs/product-scope.md` §4), not a gap discovered late — restating it here is the audit
actually checking, not assuming.

## Validation performed

```
python manage.py check                          # 0 issues
python manage.py makemigrations --check          # no missing migrations
python manage.py test csc_apps                   # 33/33 passing
```

Run against a local SQLite database for validation only (no PostgreSQL instance was
available in this environment); `csc/settings.py` itself is unchanged and still targets
PostgreSQL, matching PMS (`docs/pms-reference-analysis.md` §1). `python manage.py migrate`
against a real PostgreSQL instance was not exercised in this environment — the generated
migrations (`csc_apps/*/migrations/0001_initial.py`) are standard Django migrations with
no PostgreSQL-specific operations, so this is a low-risk gap, but it is not the same as
having actually run it, and is recorded as such.
