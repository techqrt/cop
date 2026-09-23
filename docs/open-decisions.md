# Open Architectural Decisions

Unresolved items referenced from other Phase 0 documents. None of these block Phase 0
(each has a safe interim default, noted below); they should be resolved before the
Phase 1 work that depends on them starts.

---

### OD-001 — Does a "Reviewer" role exist, and how is review assigned?
**Question:** The brief names "human review" and "officer approval" as distinct pipeline
steps but only ever says "officer" as a user type. Is review performed by the same officer
who recorded the crash, a separate Reviewer/Supervisor role, or both depending on
department policy?
**Why it matters:** Determines whether `IN_REVIEW` needs an assignee field, whether a
Reviewer role exists at all (`docs/security-baseline.md` §2), and the Dashboard's "pending
my review" view (`docs/user-workflow.md`).
**Current options:** (a) self-review only (Officer approves their own recording); (b)
mandatory separate Reviewer role; (c) configurable per deployment.
**Recommended:** (b) — matches the brief's "AI-generated data" vs. "human-approved record"
language most literally, and is the safer default for an evidentiary system (an officer
shouldn't be the sole check on their own report).
**Phase 6 status:** Narrowed, not fully closed
(`docs/phase6-officer-review-approval.md`). The owning officer OR any
REVIEWER/ADMIN may approve - option (a) and (b) both apply rather than choosing
one exclusively, the smallest change from the authorization rule
`GET /recordings/<id>/` already used. Still unresolved: whether review should
ever be *mandatory* for a second person (vs. this phase's "either may act"), and
any assignment/queue mechanism ("recordings pending my review" - no such view
exists yet).
**Status:** Open. Interim default used from Phase 0 through Phase 6: role exists
(`REVIEWER`), assignment mechanism undecided, both owner and REVIEWER/ADMIN may
approve.

### OD-002 — Which STT / translation / extraction providers?
**Question:** No provider is named in the brief for any of the three AI stages.
**Why it matters:** Gates the provider implementations under
`csc_apps/processing/providers/<provider>/`.
**RESOLVED for STT, as of Phase 2 (`docs/phase2-sarvam-stt.md`):** Sarvam AI, via the
Batch Speech-to-Text API and the official `sarvamai` SDK
(`csc_apps/processing/providers/sarvam/`). Do not re-open STT provider selection —
see ADR-015.
**RESOLVED for translation, as of Phase 3 (`docs/phase3-sarvam-translation.md`):**
Sarvam AI, model `sarvam-translate:v1` (formal style), via `POST /translate` / the
same `sarvamai` SDK
(`csc_apps/processing/providers/sarvam/translation_provider.py`). Do not re-open
translation provider or model selection — see ADR-017.
**RESOLVED for extraction, as of Phase 4 (`docs/phase4-gemini-edar-extraction.md`):**
Google Gemini, model `gemini-3.8-flash`, via structured JSON output
(`response_json_schema`) and the official `google-genai` SDK
(`csc_apps/processing/providers/gemini/`). Do not re-open extraction provider or
model selection — see ADR-018.
**Status:** Resolved for all three stages (STT, translation, extraction). This OD is
fully closed; kept here as a record of the decision and its reasoning rather than
deleted.

### OD-003 — Production async task-queue technology
**Question:** Celery+Redis/RabbitMQ, RQ, Django-Q, a cloud-native task queue (SQS+worker,
Cloud Tasks, etc.), or something else?
**Why it matters:** PMS has no precedent (`docs/pms-reference-analysis.md` §1); ADR-009's
`TaskRunner` interface is designed so this choice is swappable, but Phase 1 needs to pick
one to actually run in production.
**Current options:** Celery (mature, heavyweight, needs a broker); RQ (simpler, Redis-only);
a cloud-managed queue (less ops overhead, vendor coupling).
**Recommended:** RQ as the lightest option consistent with CSC's current infrastructure
footprint (PMS itself runs no broker today), revisit if job volume/complexity grows.
**Status:** Open. Interim default: `InlineTaskRunner` (dev-only, synchronous) ships in
Phase 0; must not be used in production.

### OD-004 — Jurisdiction lookup mechanism
**Question:** How does `police_station_jurisdiction` actually get resolved from GPS and/or
spoken text (ADR-014)?
**Why it matters:** Needs either a geofence/boundary dataset (jurisdiction polygons), an
external lookup service, or officer manual entry as the only source.
**Current options:** (a) a maintained geofence table checked against `Recording.gps_*`; (b)
an external government/GIS API; (c) manual officer selection from a dropdown, AI-assisted by
matching spoken station names.
**Recommended:** (a) for offline/low-connectivity field reliability, with (c) as a fallback
when GPS is unavailable indoors/underground.
**Status:** Open. Interim default: `police_station_jurisdiction` is a free-text field on
`Recording`, populated by whichever mechanism exists when Phase 1 builds this — no lookup
implemented in Phase 0.

### OD-005 — Who/what sets `NOT_APPLICABLE`, and AI-attempt retention
**Question:** Two related sub-questions from `docs/unknown-data-policy.md` §5 and
`docs/data-provenance.md` §3: (a) is `NOT_APPLICABLE` set by an automatic rule engine or
left to the provider/reviewer; (b) are superseded AI-extraction attempts retained for audit,
or only the most recent kept live?
**Why it matters:** Affects `EdarFieldValue` write paths and possibly a future attempt-
history table.
**Recommended:** (a) automatic rule engine for the clearly-mechanical cases (vehicle/
casualty index bounds), left to the provider elsewhere; (b) retain only most-recent live,
add an append-only attempt log later only if an actual audit requirement surfaces.
**Status:** Open. Interim default matches the "recommended" column — not yet implemented.

### OD-006 — Export format(s) and destination
**Question:** What file format(s) does the eDAR intake system expect (its own JSON schema?
a specific government portal format? CSV? PDF for filing?), and is export push (API call
to iRAD/eDAR) or pull (officer downloads a file)?
**Why it matters:** Named as a later capability in the brief with zero format detail given.
**Status:** Open. No interim default — `Export` is not modeled at all in Phase 0
(`docs/domain-model.md` §4).

### OD-007 — Exact role permission matrix and access-token lifetime
**Question:** Precisely which actions each of `OFFICER`/`REVIEWER`/`ADMIN` may perform
(`docs/security-baseline.md` §2), and whether CSC should shorten PMS's 7-day token lifetime
given the higher sensitivity of crash-scene evidence.
**Recommended:** A shorter access-token lifetime (hours, not days) with refresh, given
field-device loss risk; full matrix defined once review endpoints exist in Phase 1.
**Status:** Open. Interim default: PMS's 7-day lifetime, unchanged, flagged for revisit.

### OD-008 — Audio/media storage backend
**Question:** Local disk (parity with PMS) vs. S3/GCS/Azure Blob with private ACLs, for
production.
**Why it matters:** `docs/security-baseline.md` §3 requires private-by-default storage,
which local Django `media/` serving does not provide out of the box the way PMS uses it.
**Recommended:** Cloud object storage with private ACLs + short-lived signed URLs for
production; local disk acceptable for local development only.
**Status:** Open. Interim default: `Audio.storage_path` is an opaque string key,
backend-agnostic by design (`docs/domain-model.md`), so this choice doesn't touch the
domain model when made.

### OD-009 — Retry attempt limits and backoff, and automatic vs. manual retry trigger
**Question:** Is `max_attempts=3` (`docs/error-retry-strategy.md` §2) right for every
provider/stage, should it be configurable, and does a `FAILED` job retry automatically on a
schedule or only when an officer/admin explicitly retries?
**Recommended:** Configurable per job type once real failure data exists; automatic retry
with backoff for `is_retryable=True` failures, manual-only for anything that required a
human classification override.
**Status:** Open. Interim default: fixed `max_attempts=3`, manual retry trigger only (no
scheduler in Phase 0).

---

### OD-010 — Audio upload size limit and format allowlist
**Question:** What is the real maximum crash-scene recording size/duration, and which
audio formats do actual field devices produce?
**Why it matters:** `csc_apps/recordings/validators.py` (Phase 1,
`docs/phase1-audio-ingestion.md` §4) had to pick concrete numbers to be able to reject
oversized/unsupported uploads at all.
**Current interim default:** 100 MB (`AUDIO_MAX_UPLOAD_SIZE_BYTES`, configurable),
`.wav`/`.mp3`/`.m4a`/`.aac`/`.ogg`/`.webm`/`.flac` (fixed allowlist in code, not
per-deployment configurable at the individual-format level).
**Recommended:** Revisit both once real officer-device audio samples/telemetry exist -
the 100 MB figure is a conservative ceiling reasoned from "a 10-minute uncompressed WAV
recording" (`eDAR Fields.pdf`'s own "within 10 minutes at the scene" scoping note), not
measured data.
**Status:** Open.

### OD-011 — Request-retry idempotency mechanism
**Question:** Should the upload endpoint deduplicate an identical retried request
before any write (e.g. a client-supplied `Idempotency-Key` header checked
server-side), rather than the lighter signal Phase 1 implements?
**Why it matters:** A naive mobile client retry (timeout, double-tap) could otherwise
silently create two `Recording`s for the same audio.
**Current interim implementation:** Not prevented. A same-officer, same-checksum
upload within a 5-minute window still ingests normally, but the response's
`possibleDuplicateOfRecordingId` field names the likely-duplicate prior recording
(`docs/phase1-audio-ingestion.md` §14) - a safe-minimum, informational signal, not a
distributed idempotency-key system.
**Recommended:** Add a proper `Idempotency-Key` mechanism only if duplicate-recording
volume in practice justifies the added complexity - premature otherwise.
**Status:** Open.

### OD-012 — Authenticated audio retrieval/download endpoint
**Question:** Does any Phase 1+ consumer need to fetch stored audio bytes over HTTP
(vs. Phase 2's STT step reading directly via `AudioStorage.open()` in-process)?
**Why it matters:** Determines whether a `GET` endpoint (and its own authorization
rules) needs to exist at all.
**Current interim implementation:** No such endpoint exists
(`docs/phase1-audio-ingestion.md` §13) - deliberately deferred, not overlooked.
**Recommended:** Add one only when a concrete consumer needs it (e.g. an officer-facing
playback feature), scoped with authorization from day one.
**Status:** Open.

### OD-013 — eDAR categorical value enumerations
**Question:** `schemas/edar-schema.json` marks most categorical fields (road_type,
crash_type, primary_causation_factor, vehicle_type, and ~26 others) as
`allowed_values_status: "unresolved_open_decision"` - the eDAR source document
(`eDAR Fields.pdf`) specifies these are categorical without enumerating the actual
category list. Only `injury_severity` has a real, source-defined enum.
**Why it matters:** Phase 4's Gemini extraction (`docs/phase4-gemini-edar-extraction.md`
§eDAR schema integration) directly depends on this - without defined enums, Gemini
extracts free-text descriptive phrases for these fields ("sharp bend", "National
Highway") rather than a closed category, which is workable but means two different
extractions of similar transcripts could describe the same category with different
wording, making these fields harder to aggregate/report on than a true enum would be.
**Current interim implementation:** Free-text for every field without a resolved enum,
enum-constrained only where one is defined
(`csc_apps/processing/providers/gemini/schema_adapter.py`, ADR-018 point 5) - not a
silent invention of category lists Phase 0 deliberately left open.
**Recommended:** Define the actual category lists per field, sourced from the
official eDAR/iRAD field specification (not invented ad hoc), then update
`schemas/edar-schema.json`'s `allowed_values`/`allowed_values_status` for each -
Gemini's schema and the deterministic validator will pick up a resolved enum
automatically once present (no code change needed beyond the schema data itself).
**Phase 5 status (still OPEN, deliberately not resolved):** Phase 5 validation
(`docs/phase5-validation-provenance.md` §8) checks types, the one resolved enum
(`injury_severity`), confidence, evidence and counts. It does NOT validate the value of
any categorical field marked `unresolved_open_decision` - those are accepted as free
text and merely counted (`freeTextCategoricalFields`). Still unresolved: (1) the
category list for each of the ~26 unresolved fields; (2) whether a closed list or
free text is the product decision per field; (3) whether Gemini's free-text should be
normalised/mapped to a closed list later; (4) how `known` should behave for a value that
fits no category. Resolution still requires only schema data, then validation tightens
automatically.
**Status:** Open.

---

## How to resolve one of these

Update this file's `Status` line for the item, then propagate the resolution into the
document(s) that reference it (each OD lists which). Do not silently start building against
an unresolved OD as if it were decided — that's exactly the failure mode this file exists to
prevent.
