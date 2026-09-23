# Phase 9 — Security + Audit + Observability Hardening

## 1. Purpose

Audits and hardens Phases 0-8 before Phase 10's production QA/deployment. No new
business functionality, no new eDAR fields, no change to STT/translation/extraction/
approval/export behavior beyond what a genuine, minimal security fix required (none
was — every change in this phase is additive: new audit entries, new settings, new
tests, corrected documentation).

## 2. Method

A full read-only audit pass (authentication, authorization, `ActivityLog`,
processing observability, logging, configuration, serializers, storage, error
handling, secret scanning) preceded any code change, per the checklist in the
source instructions §4. Findings and their resolution are in §3 below; items with
no finding are listed as "verified, no change."

## 3. Security Audit Summary

| Area | Finding | Resolution |
|---|---|---|
| Login auditing | No `ActivityLog` entry on login | Added (`action='Read', model='User'`) |
| Export auditing | Phase 8 explicitly deferred this | Added (`action='Read', model='EdarRecord'`) |
| `SECRET_KEY` default | Falls back to a value committed in this repo; nothing stopped running `DEBUG=False` with it | `ImproperlyConfigured` raised at startup if `DEBUG=False` and the default is still in use |
| `docs/security-baseline.md` | Said only REVIEWER/ADMIN write APPROVED - stale, contradicts Phase 6's actual (confirmed) decision | Corrected in place, matrix added |
| Security headers | `SECURE_CONTENT_TYPE_NOSNIFF`/`SECURE_REFERRER_POLICY`/`X_FRAME_OPTIONS` not explicitly set | Set explicitly (all TLS-independent, safe under any deployment) |
| `ALLOWED_HOSTS` / TLS settings / CORS | Permissive/unset defaults | Deployment-dependent - documented, not force-changed (§12) |
| IDOR (detail/list/approve/export) | Already enforced per-endpoint since Phase 2/6/7/8 | Verified again with one consolidated cross-endpoint test |
| Identity spoofing (officer, `reviewed_by`) | No request serializer exposes these fields | Verified with regression tests proving extra body keys are ignored |
| Provenance/layer injection | `ApproveEdarRequestSerializer` has no `layer`/`confidence`/`evidence`/`extraction_version` field | Verified with regression tests; structural scan confirms only `approval_service.py` ever constructs a `layer='APPROVED'` row |
| Logging secret leakage | Provider SDK credentials never logged (already true - Phase 2/4's own docstrings already claimed this) | Verified with a real `assertLogs` test plus an AST-based structural scan of every `logger.*()` call site |
| Error message leakage | `Common().exception_handler` already routes unexpected exceptions through `Utils.env_exception_handler` (DEBUG-gated) | Verified with regression tests forcing a real exception through a live endpoint |
| Upload path traversal | Already fixed at the validator/storage layer (Phase 1) | Verified end-to-end through the real HTTP upload path (not just the validator unit) |
| Query security (Phase 7) | Whitelisted `ChoiceField`s, parameterized ORM filters, no raw SQL | Verified by inspection; no `.raw()`/`cursor.execute()`/f-string SQL anywhere |
| Hardcoded secrets in source | None found | Repository-wide pattern scan, clean |

## 4. Authentication

Inspected `csc_apps.authentication.authentication.JWTAuthentication` and
`AuthView`. No architectural change - the existing HS256 JWT + DB-stored
single-active-session token (a login overwrites `User.access_token`, invalidating
every prior token) is sound and unchanged. Added regression tests: missing token
(401), malformed token (403), a token with a genuinely wrong signature - including
one attempting to impersonate a different `user_id` (403), and confirmation that
`request.params.user_id` always comes from the authenticated `request.user`, never
from the request body. Login now writes an `ActivityLog` entry (`action='Read'`)
containing only `{'event': 'login'}` - never the token or password. Failed login
attempts are **not** tracked (see §15 Remaining Risks).

## 5. Authorization

See `docs/security-baseline.md`'s corrected resource/role matrix (reproduced
below). No permission was broadened or narrowed in this phase - the matrix
documents Phase 2-8's actual, already-implemented behavior, correcting one stale
claim in the original Phase 0 doc.

| Resource/action | Owner | REVIEWER | ADMIN | Other officer |
|---|---|---|---|---|
| Upload | ✅ | ✅ | ✅ | n/a (every role may upload) |
| List/history | own only | own only | own only | own only |
| Detail | ✅ | ✅ (any) | ✅ (any) | ❌ |
| Approve | ✅ | ✅ (any) | ✅ (any) | ❌ |
| Export | ✅ | ✅ (any) | ✅ (any) | ❌ |

## 6. IDOR

Every protected endpoint already enforced authorization before this phase
(`RecordingView._get_authorized_recording`, reused by detail/approve/export; the
list endpoint's own `Recording.objects.filter(officer=requesting_user)`). Phase 9
adds `ConsolidatedIdorTests` - one "other officer" identity checked against
detail, list, approve, and export in a single place, plus a check that filtering
by another officer's exact `case_fir_number` cannot be used to detect that their
recording exists. All authorization is applied at the database query level before
any pagination or data is returned - never fetch-then-filter in Python.

## 7. ActivityLog / Audit

Actions now audited: recording creation (`Create`, already existed since Phase
1), eDAR approval with field-level changes (`Update`, already existed since Phase
6), login (`Read`, **new**), eDAR export (`Read`, **new**). `ActivityLog` remains
the single audit mechanism - no second table was introduced.

**Export auditing (Phase 8's deferral, resolved):** `RecordingView.
export_edar_extract` calls `ActivityLog.record(user=requesting_user,
action='Read', model='EdarRecord', details={'event': 'export', 'recording_id':
..., 'edar_record_id': ...})` immediately after a successful export is built.
`details` carries only two integer IDs and an event name - never a field value,
never the eDAR payload. Verified with a test that plants a field value
(`'SECRET-ROAD-NAME'`) and confirms it never appears in the resulting audit
entry. A failed/unauthorized export attempt creates no entry at all.

The `'Read'` action required one migration (`activity_log.0003_alter_
activitylog_action`) - no fitting verb existed in the original three-choice set;
adding it was judged a legitimate requirement, not something to route around
(source instructions §10/§33).

## 8. Sensitive Data

Prohibited from logs, `ActivityLog.details`, and API responses alike: JWTs,
passwords, `Authorization` headers, provider API keys (`SARVAM_API_KEY`,
`GEMINI_API_KEY`), raw audio bytes, full transcript text, the complete eDAR
payload, AI provenance fields (`confidence`/`evidence`/`extraction_version`) in an
audit entry, and Gemini/Sarvam raw responses. Enforced by: `Utils.
get_query_params`/serializers never touching these fields; every `ActivityLog.
record()` call site in this codebase passing only IDs/counts/event names; every
`logger.*()` call in the processing services passing only `job_id`/`recording_id`/
`error_code`/status/duration (never `str(error)` or a provider's raw message
body) - verified with both a runtime `assertLogs` test and an AST-based structural
scan of every logger call site, not a spot check.

## 9. Observability

`ProcessingJob.status` (`PENDING`/`RUNNING`/`RETRYING`/`SUCCEEDED`/`FAILED`) and
`ProcessingEvent` (one row per milestone: `*_started`/`*_succeeded`/`*_failed`,
plus `*_job_created` for automatic chaining) already fully distinguish every
processing state across STT/translation/extraction - unchanged, verified by
inspection and the already-passing Phase 2-5 retry/failure test suites. No
dashboard, no duplicate status system, no distributed tracing was added (none of
the three is in scope for Phase 9 per the source instructions, and none was
judged necessary - `job_id`/`recording_id` already thread through every log line
and event as the de facto correlation key; no new mechanism was introduced).

## 10. Error Handling

`csc_apps.common.common.Common.exception_handler` already routed unexpected
exceptions through `Utils.env_exception_handler`, which returns the real message
only when `Configurations.debug` is true and a fixed `Constants.server_error`
otherwise - this was already correct, not changed. `ValueError`/business-rule
messages are always echoed verbatim by design (they're developer-authored, safe,
generic strings - e.g. "Recording not found" - never raw exception internals).
Verified with two new regression tests: a genuine `RuntimeError` containing a
fabricated sensitive string, forced through a real HTTP request to `GET
/recordings/<id>/`, confirms neither the sensitive string nor a Python traceback
ever reaches the client, under both `DEBUG=False` and `DEBUG=True`.

## 11. Upload Security

Filename-based path traversal (`../../../../etc/passwd.wav`) and storage-key
generation from the original filename were already prevented since Phase 1
(`validate_audio_upload` sanitizes to basename; `LocalPrivateAudioStorage`
generates a UUID-based key scoped by `recording_id`, never the filename). Phase 9
adds two end-to-end tests through the *real* HTTP upload path (Phase 1's own
tests only exercised the validator function directly) confirming the stored
`Audio.storage_path` never contains `..`, the traversal target, or the original
filename. Upload size limits and audio remaining under private, non-static
storage are unchanged.

## 12. Configuration Security

**Application-level (implemented in this phase):**
- `SECURE_CONTENT_TYPE_NOSNIFF = True`, `SECURE_REFERRER_POLICY = 'same-origin'`,
  `X_FRAME_OPTIONS = 'DENY'` - safe under any deployment topology.
- `SECRET_KEY` insecure-default guard (`ImproperlyConfigured` if `DEBUG=False` and
  the default is still in use).

**Deployment-dependent (deferred to Phase 10, documented not implemented):**
- `ALLOWED_HOSTS` defaults to `'*'` - must be set to the real production host(s).
- `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
  `SECURE_HSTS_SECONDS` - all assume TLS is terminated somewhere in front of this
  app; forcing them now would break local/test HTTP access. Enable once the
  actual production TLS topology is known.
- CORS: no package installed, no `CORS_*` settings. No frontend origin is defined
  yet to configure against; add `django-cors-headers` only once a real frontend
  origin exists.
- CSRF: Django's `CsrfViewMiddleware` is enabled (default), but every endpoint in
  this API is registered via DRF's `@api_view`, which DRF marks CSRF-exempt
  automatically regardless of authentication class. This is correct for a
  stateless Bearer-token API (CSRF requires ambient, browser-attached credentials
  like cookies, which this API's auth does not use) - not a gap, and not something
  to "fix" by weakening it further.
- `DEBUG` in the actual deployed environment: the server used for this project's
  own live smoke testing was observed running with `DEBUG=True` - flagged loudly
  here since it directly interacts with the `SECRET_KEY` guard added in this
  phase (§14).

## 13. Files Created

- `csc_apps/activity_log/migrations/0003_alter_activitylog_action.py`
- `csc_apps/recordings/test_phase9_security.py`
- `docs/phase9-security-audit-observability.md`

## 14. Files Modified

- `csc_apps/activity_log/models/activity_log.py` - added `'Read'` action choice.
- `csc_apps/authentication/views.py` - login audit entry.
- `csc_apps/recordings/views.py` - export audit entry.
- `csc/settings.py` - `SECRET_KEY` guard, three safe security headers.
- `csc_apps/authentication/tests.py`, `csc_apps/activity_log/tests.py` - new tests.
- `docs/security-baseline.md` - corrected the stale APPROVED-authorization claim, added the resource/role matrix and logging updates.
- `docs/architecture-decisions.md` - ADR-023.

## 15. Remaining Security Risks / Deployment Requirements

**Fixed in Phase 9:**
- Login and export are now auditable.
- `SECRET_KEY` can no longer silently run insecure with `DEBUG=False`.
- Stale authorization documentation corrected.
- Three safe security headers set explicitly.

**Requires Phase 10 deployment work (not implemented here, by design):**
- Set a real `ALLOWED_HOSTS` for the production domain.
- Set `DEBUG=False` on the actual deployed server (currently observed `True`).
- Terminate TLS and enable `SECURE_SSL_REDIRECT`/`SESSION_COOKIE_SECURE`/
  `CSRF_COOKIE_SECURE`/HSTS once the topology is known.
- Configure CORS once a real frontend origin exists.
- Rotate the Sarvam/Gemini API keys used during this project's development and
  smoke testing, since they were shared in conversation at least once during
  earlier phases - no evidence they leaked into source control, logs, or any API
  response (repeatedly verified), but rotation is still the safe default for a
  credential that passed through a chat transcript.

**Deliberately not built (scope boundary, not an oversight):**
- Failed-login-attempt tracking / brute-force detection - edges into
  intrusion-detection infrastructure the source instructions explicitly warn
  against building in Phase 9.
- A second `SecurityEvent`/`LoginHistory`/`ExportHistory` table - `ActivityLog`
  already covers the requirement.
