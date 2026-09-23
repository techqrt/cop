# Security Baseline

## 1. Authentication

- Credentials: **email + password** (SOURCE REQUIREMENT), not PMS's phone-number-based
  login. Passwords hashed via Django's standard `AbstractBaseUser` + `set_password`/
  `check_password` (PBKDF2 by default) — not PMS's pattern to deviate from, PMS just never
  needed to be inspected here since its own `User` model doesn't expose a password flow in
  the files reviewed; this is standard Django, not a PMS convention.
- Token issuance/verification: **adopted from PMS** (`docs/pms-reference-analysis.md` §7) —
  a JWT (HS256, `PyJWT`) is issued on login and stored on `User.access_token`; every request
  is authenticated by decoding the Bearer token *and* comparing it against the DB-stored
  value. This gives single-active-session behavior for free: issuing a new token (a second
  login) invalidates the first token immediately, without needing a blacklist.
- Token lifetime: PMS uses a 7-day access token with no separate short-lived
  access/long-lived refresh split in practice (both `ACCESS_TOKEN_LIFETIME` and
  `REFRESH_TOKEN_LIFETIME` are 7 days). CSC's exact lifetime is an **ARCHITECTURAL
  DECISION**, not a hard requirement — Phase 0 keeps PMS's 7-day value as a starting default
  but flags it in `docs/open-decisions.md` OD-007 since a field officer's device being lost
  is a more acute risk for CSC (crash evidence) than for PMS (property records).

## 2. Roles and authorization

Flat role field on `User`: `OFFICER | REVIEWER | ADMIN` (ASSUMPTION — see
`docs/open-decisions.md` OD-001 for why a Reviewer role exists despite not being named in
the source brief, and OD-007 for the exact permission matrix per role). This replaces PMS's
department-string permission matrix (`docs/pms-reference-analysis.md` §7), which doesn't fit
CSC — there is one domain (crash reporting), not many departments.

Baseline authorization rules, as actually enforced by Phase 2-8 (superseding this section's
original Phase 0 speculation below where the two disagree — see the note at the end of this
section):
- A recording's `Audio`, `Transcript`, and `EdarRecord` are visible only to its owning
  `Officer` and to `Reviewer`/`Admin` roles — never to another Officer.
- **Corrected from Phase 0's original text below:** the owning `Officer` — not only
  `Reviewer`/`Admin` — may write to the `APPROVED` layer of `EdarFieldValue`, via
  `POST /recordings/<id>/edar/approve/`. This was an explicit Phase 6 product decision
  (`docs/phase6-officer-review-approval.md`, ADR-020), confirmed again for Phase 9
  (`docs/phase9-security-audit-observability.md` §4) rather than silently left inconsistent
  with this document.
- `Admin` is the only role permitted to hard-delete a `Recording` (and, per ADR-002, even
  `Admin` cannot delete a `Recording` with an `Audio` row via cascade — deletion of evidence
  requires an explicit, separately-audited action, not a default `DELETE` endpoint). No
  delete endpoint exists as of Phase 9.

### Resource/role matrix (Phase 2-8, as implemented)

| Resource/action | Owner | REVIEWER | ADMIN | Other officer |
|---|---|---|---|---|
| Upload (`POST /recordings/`) | ✅ (becomes owner) | ✅ (becomes owner) | ✅ (becomes owner) | n/a — every authenticated role may upload (OD-007 interim default) |
| History/list (`GET /recordings/`) | own only | own only | own only | ✅ own only |
| Detail (`GET /recordings/<id>/`) | ✅ | ✅ (any recording) | ✅ (any recording) | ❌ |
| Approve (`POST /recordings/<id>/edar/approve/`) | ✅ | ✅ (any recording) | ✅ (any recording) | ❌ |
| Export (`GET /recordings/<id>/export/`) | ✅ | ✅ (any recording) | ✅ (any recording) | ❌ |

The list/history scope (own recordings only, for every role including REVIEWER/ADMIN) is
deliberately narrower than detail/approve/export (owner OR REVIEWER/ADMIN) — a confirmed
Phase 7 product decision (`docs/phase7-history-search.md` §4, ADR-021), not an
inconsistency: a REVIEWER/ADMIN can still open, approve, or export any recording directly
by ID, they just don't see other officers' recordings in their own list.

## 3. Audio storage

- **Private by default.** PMS serves `media/` directly (and even exposes it outside `DEBUG`
  via `static()` conditionally, but with no per-object access control at all) — this is
  explicitly called out as a pattern **not** adopted for CSC, because crash-scene audio is
  materially more sensitive than a property-management profile photo. Audio must be served
  through an authenticated, authorized endpoint (or short-lived signed URL from whichever
  storage backend is eventually chosen), never a static file path.
- **No accidental public access.** The concrete storage backend (local disk for Phase 0
  parity with PMS, vs. S3/GCS with private ACLs for production) is
  `docs/open-decisions.md` OD-008 — Phase 0's `Audio.storage_path` field is
  backend-agnostic (a string key, not a public URL) precisely so this choice doesn't leak
  into the domain model.

## 4. AI credentials

- Provider API keys live only in server-side environment configuration (`csc/config.py`,
  following PMS's `python-decouple` + `Configurations` pattern), never in frontend code or
  committed to source control. As of Phase 2 this is no longer hypothetical:
  `SARVAM_API_KEY` (`docs/phase2-sarvam-stt.md` §12/§14) is read via `Configurations.
  sarvam_api_key` and passed explicitly to the `sarvamai` SDK client — never left to the
  SDK's own `os.getenv` fallback, never logged, never returned by any API response.
  Phase 3's `SarvamTranslationProvider` reuses this exact same credential and pattern
  (`docs/phase3-sarvam-translation.md` §16) — no second Sarvam credential was
  introduced. Phase 4 introduces one new credential, `GEMINI_API_KEY`, for Google
  Gemini, following the identical pattern (`Configurations.gemini_api_key`, never
  logged, never returned — `docs/phase4-gemini-edar-extraction.md` §18). All three AI
  provider selections (OD-002) are now resolved. Phase 5 adds no credential and no
  provider call. It does start returning per-field `evidence` (a short excerpt of the
  English transcript) through the existing `GET /recordings/<id>/`, under the same
  Recording authorization as the transcript itself (owner, REVIEWER, ADMIN); validation
  issue messages are generic and value-free, so validation output never leaks a value or
  transcript text beyond that (`docs/phase5-validation-provenance.md` §13).
- `.env` is git-ignored (`.gitignore`); `.env.example` documents required variable *names*
  with no real values. PMS's own working tree has a local `.env` with real database
  credentials, but it is excluded via PMS's `.gitignore` and was not read into or copied by
  any CSC file (verified: `docs/pms-reference-analysis.md` §1 only lists the variable
  *names*). CSC's `.gitignore` excludes `.env` from day one on the same basis.

## 5. Data authorization

Every domain query (Recording, Audio, Transcript, ProcessingJob, EdarRecord,
EdarFieldValue) must be scoped by the requesting user's role and ownership — no endpoint
returns cross-officer data by default. This is the same shape as PMS's per-request
ownership/assignment checks in `LeadView` (`docs/pms-reference-analysis.md` §3), adapted
from "assigned lead" to "recording owner."

## 6. Logging

- Never log secrets: JWTs, passwords, provider API keys, and raw `Authorization` headers are
  never written to application logs.
- Audio and transcript **content** is not logged at INFO level or below — only IDs,
  durations, and status transitions (`docs/observability.md`). A DEBUG-level log of
  transcript content, if ever needed for troubleshooting, must be explicitly
  scoped/redactable and off by default, since a crash-scene transcript can contain names,
  medical detail, and other sensitive personal information.
- Verified for Phase 9 (`docs/phase9-security-audit-observability.md` §Logging): every
  `logger.*()` call in `csc_apps.processing.{stt,translation,extraction}_service` passes
  only job/recording IDs, the controlled `error_code`, status, and duration/count metrics —
  never a raw provider exception message, never transcript/eDAR content. A structural test
  (AST-based, not just a spot check) guards against a future call site regressing this.
- `ActivityLog` now also covers login (`action='Read', model='User'`) and eDAR export
  (`action='Read', model='EdarRecord'`) — see `docs/phase9-security-audit-observability.md`
  §ActivityLog. Neither entry stores the token/password or the exported payload; `details`
  carries only an `event` name and relevant IDs.

## 7. What Phase 0 implements vs. documents

Implemented: `User` model with email+password+role, `JWTAuthentication` (PMS pattern
ported), `.env`/`.gitignore` secret handling, `Audio.storage_path` as an opaque backend-
agnostic key. Documented only: the concrete storage backend, per-endpoint authorization
enforcement (endpoints don't exist yet), and the exact role permission matrix
(`docs/open-decisions.md` OD-007).
