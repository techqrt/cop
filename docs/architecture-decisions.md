# Architecture Decision Records

Status legend: **Accepted** (in force for Phase 0), **Open** (see `docs/open-decisions.md`
for the still-unresolved parts of an otherwise-accepted decision).

---

### ADR-001 — PMS is the reference engineering standard, not a functional dependency
CSC follows PMS's layering (controller -> view -> model), error handling, serializer/
dataclass split, response envelope, and testing conventions (`docs/pms-reference-analysis.md`).
CSC does not import PMS code, share its database, or reuse any PMS domain logic.
**Status:** Accepted.

### ADR-002 — Audio is preserved as immutable raw evidence
Once stored, an audio file's bytes and its `Audio` row's core evidentiary fields are never
overwritten by any pipeline stage. Consequence: `on_delete=models.PROTECT` (not PMS's usual
`DO_NOTHING`) on the FK chain from `EdarRecord`/`ProcessingJob` back to `Audio`/`Recording`,
so no application code path can cascade-delete evidence. **Status:** Accepted.

### ADR-003 — Live recording and upload converge into one pipeline
Both input paths produce an `Audio` row and, once that row exists, are indistinguishable to
every downstream stage (STT, translation, extraction). The only difference is how the
`Audio` row gets created (`source = LIVE | UPLOAD`). **Status:** Accepted.

### ADR-004 — Translation is mandatory, not optional
Every `Recording` produces exactly one original-language transcript and exactly one English
translation before extraction runs; extraction always reads the English translation, never
the original-language transcript directly. **Status:** Accepted.

### ADR-005 — Single-speaker audio is assumed
No diarization field, no speaker-turn model. `Transcript` is one continuous text (with
timestamp segments for provenance), not a list of per-speaker utterances.
**Status:** Accepted.

### ADR-006 — eDAR extraction uses a fixed, versioned schema
`schemas/edar-schema.json` is the single source of truth for what fields exist, their types,
and (where the source document specifies them) their allowed values. The extraction
provider is given this schema and must not invent fields; the persistence layer validates
every extraction result against it before storing. **Status:** Accepted.

### ADR-007 — AI output is structured JSON, never a canonical Markdown table
The extraction provider interface (`ExtractionProvider.extract`) returns a typed structure
(field -> value/confidence/provenance), not free text. A UI may render that structure as a
table; the table is a view, not the data. **Status:** Accepted.

### ADR-008 — AI-generated data and officer-approved data remain separate
Modeled as two layers on `EdarFieldValue`: `layer = AI | APPROVED`. Approving a record
creates/updates the `APPROVED` layer; it never mutates the `AI` layer's rows. An officer
edit after approval updates only the `APPROVED` layer. See `docs/data-provenance.md`.
**Status:** Accepted.

### ADR-009 — Long-running AI operations are asynchronous
PMS has no background-processing precedent (`docs/pms-reference-analysis.md` §1), so this is
a new abstraction for CSC: a `TaskRunner` interface (`csc_apps/processing/tasks/`) decoupled
from any specific queue technology. Phase 0 ships one implementation, `InlineTaskRunner`,
explicitly marked dev-only (runs the job synchronously in-process) so the system is
runnable end-to-end locally without standing up infrastructure. The production queue
technology is **Open** — see `docs/open-decisions.md` OD-003. **Status:** Accepted
(abstraction) / Open (production implementation).

### ADR-010 — Extracted values carry provenance
Every `EdarFieldValue` in the `AI` layer stores `confidence`, `source_transcript_segment`,
`source_start_time`, `source_end_time`, and `extraction_version`. A value with no supporting
transcript evidence is stored as `KNOWN=false` (see `docs/unknown-data-policy.md`) and must
not carry a fabricated confidence score. **Status:** Accepted.

### ADR-011 — eDAR field values are stored generically, not as 42 fixed columns
Rejected approach: one column per eDAR field on `EdarRecord`/`Vehicle`/`Casualty`. Adopted
approach: a single `EdarFieldValue` row per `(record, field_key, layer)`, where `field_key`
namespaces repeating groups (`vehicle.2.registration_number`, `casualty.1.injury_severity`).
Rationale: the schema is expected to evolve (categorical enumerations are explicitly marked
unresolved in `eDAR Fields.pdf`), and a fixed-column table would require a migration for
every schema change; a generic field-value store validated against
`schemas/edar-schema.json` does not. This also makes ADR-008's two-layer model and ADR-010's
per-field provenance fall out naturally (both are properties of a field-value row) rather
than requiring a parallel shadow table per entity. **Status:** Accepted — see
`docs/domain-model.md` for the full schema.

### ADR-012 — GPS is device-captured data, not an AI extraction target
`Recording.gps_latitude`/`gps_longitude` are written directly from the client's device GPS
at recording-creation time. The AI pipeline never populates or overrides them. Module A's
"GPS coordinates" field in the eDAR schema is sourced from `Recording`, not from
`EdarFieldValue` — see `docs/domain-model.md` §GPS and `eDAR Fields.pdf` p.1 ("Auto-captured
by phone GPS"). **Status:** Accepted.

### ADR-013 — `serilizers/` typo is not propagated
PMS's directory name (`serilizers`) is a consistent misspelling, not a convention. CSC uses
the correct spelling (`serializers/`). Purely cosmetic; no functional deviation from the
PMS pattern it's copied from. **Status:** Accepted.

### ADR-014 — Jurisdiction resolution is a pluggable lookup, not an LLM responsibility
`Recording.police_station_jurisdiction` is resolved by a dedicated `JurisdictionResolver`
interface (GPS and/or spoken text as input), not asked of the extraction LLM as a free-text
field. Phase 0 defines the interface only; the concrete lookup mechanism (geofence table,
external service, or manual officer entry as a fallback) is **Open** — see
`docs/open-decisions.md` OD-004. **Status:** Accepted (interface) / Open (implementation).

### ADR-015 — STT is Sarvam's Batch API via the official SDK, not the sync REST endpoint
Verified against https://docs.sarvam.ai (2026-09, `docs/phase2-sarvam-stt.md` §3):
Sarvam's synchronous `POST /speech-to-text` caps audio at 30 seconds, which Phase 1's
own upload size/duration reasoning already exceeds for realistic crash-scene
statements. The Batch API (job create -> upload -> start -> poll -> download, up to 2
hours/file) is the only documented fit. Its raw "start job" REST contract is not
published outside the official `sarvamai` SDK, so this is an SDK integration
(`csc_apps/processing/providers/sarvam/`), not a hand-rolled HTTP client — reverse-
engineering an unpublished endpoint would mean guessing the provider contract, which
Phase 2's source instructions explicitly forbid. Resolves `docs/open-decisions.md`
OD-002 for STT specifically (translation/extraction provider selection remains open).
**Status:** Accepted.

### ADR-016 — STT implements only the in-job bounded retry tier, not the Recording-level tier
`docs/error-retry-strategy.md` §2 actually describes **two** retry tiers, not one:
(a) an in-job, budget-bounded retry — `ProcessingJob.status` cycling `RUNNING ->
RETRYING -> RUNNING` on the *same* row while `attempt_count < max_attempts`, exactly
what its own `STATUS_CHOICES`/`attempt_count`/`max_attempts` fields are shaped for;
and (b) a *Recording*-level retry, invoked after a job has gone terminally `FAILED`
(attempts exhausted or non-retryable), which moves `Recording.status` through
`FAILED -> RETRY -> PROCESSING` and — per that document's own text — creates a **new**
`ProcessingJob` row for a fresh attempt.

Phase 2's `csc_apps.processing.stt_service.run_stt_job` implements tier (a) only: a
retryable failure with attempts remaining sets `RETRYING` (picked up again by the next
`process_pending_stt_jobs` run); once attempts are exhausted or the failure is
non-retryable, the job stays terminally `FAILED`. Tier (b) — an officer/admin action
or scheduler that creates a fresh `ProcessingJob` for a recording whose existing job
already terminally failed — is **not implemented in Phase 2** (no endpoint or command
does this). This is a real, acknowledged gap (`docs/phase2-sarvam-stt.md` §16), not a
design decision to skip it permanently — recorded here so a later phase implementing
it has this ADR to build against rather than rediscovering the two-tier model.
**Status:** Accepted (tier a, implemented) / Open (tier b, deferred).

### ADR-017 — Translation provider is Sarvam (`sarvam-translate:v1`); STT and translation are separate stages, never speech-to-English
Resolves `docs/open-decisions.md` OD-002 for translation (STT was already resolved,
ADR-015). Three related decisions, recorded together because they're one product
choice:

1. **Provider/model: Sarvam, `sarvam-translate:v1`** (not `mayura:v1`) - formal-style
   translation, appropriate for official/professional crash-scene reporting, and the
   only Sarvam translation model supporting all 22 scheduled Indian languages rather
   than `mayura:v1`'s 12. Verified current against https://docs.sarvam.ai (2026-09,
   `docs/phase3-sarvam-translation.md` §3) - `POST /translate`, 2,000-character input
   limit, formal mode only for this model. Not silently switched to `mayura:v1`.
2. **Translation happens on already-transcribed text, not via speech-to-English.**
   The pipeline is `Audio -> STT -> Original Transcript -> Translation -> English
   Transcript`, never a direct speech-to-English mode. This preserves the original
   transcript as an independently auditable artifact (an officer or reviewer can
   verify the English text against what was actually said, in the language it was
   said in), keeps STT and translation independently retryable (ADR-016) without
   coupling one provider call to two concerns, and allows re-translating from the
   same original transcript later (e.g. if a better/cheaper translation model
   becomes available) without ever touching the audio or re-running STT.
3. **Original and English transcripts are stored as two separate `Transcript` rows**
   (`language=ORIGINAL` / `language=ENGLISH`), never collapsed into one field or one
   row - Phase 0's `unique_together(recording, language)` already modeled this
   correctly; Phase 3 needed no schema change (`docs/phase3-sarvam-translation.md`
   §Transcript persistence).

**Status:** Accepted.

### ADR-018 — Extraction provider is Gemini (`gemini-3.8-flash`); AI output is a validated candidate, never auto-approved
Resolves `docs/open-decisions.md` OD-002 for extraction (STT and translation were
already resolved, ADR-015/ADR-017).

1. **Provider/model: Google Gemini, `gemini-3.8-flash`** - verified current against
   ai.google.dev and the installed `google-genai` 2.24.0 SDK source (2026-09,
   `docs/phase4-gemini-edar-extraction.md` §Gemini model), chosen over
   `gemini-3.1-pro-preview` specifically because "preview" signals non-GA support
   status, a meaningful risk for a system feeding legal/insurance-relevant crash
   reports. `gemini-3.8-flash` is stable/GA and supports structured JSON output via
   `response_json_schema`.
2. **Extraction reads only the persisted `Transcript(language=ENGLISH)`** - never
   raw audio, never the original-language transcript, never a summary of the
   transcript (`docs/phase4-gemini-edar-extraction.md` §Input, §9, §75 in the source
   instructions). Consistent with ADR-017's staged pipeline: each stage consumes
   only the previous stage's already-validated, already-persisted output.
3. **Gemini output is a candidate, deterministically validated before persistence,
   never auto-approved.** The pipeline is `English Transcript -> Gemini -> AI
   candidate (raw JSON) -> Layer 1 structural/type validation -> Layer 2 domain
   validation -> persisted EdarFieldValue rows, layer=AI`. A field Gemini has no
   evidence for is persisted as `known=UNKNOWN`, never a guessed value; the whole
   candidate is rejected together if any field fails validation (no partial AI
   eDAR is ever persisted). No confidence threshold, however high, promotes a field
   to `layer=APPROVED` - that requires an actual officer action, which does not
   exist yet (a later phase).
4. **The existing generic `EdarFieldValue(field_key, layer, known, value,
   confidence, source_transcript_segment, extraction_version)` store (ADR-011) is
   reused unchanged** - no new model, no fixed eDAR columns. Vehicle/casualty
   repetitions use the same `vehicle.<n>.<field>`/`casualty.<n>.<field>` key
   namespacing Phase 0 already designed, with Gemini's own response schema capping
   `vehicles` at 3 items (`schemas/edar-schema.json`'s `max_repetitions`) and
   `resolve_field_key`'s existing bounds check as a second, independent enforcement
   of the same limit.
5. **Categorical fields without a defined enum stay free-text.** Most of
   `schemas/edar-schema.json`'s categorical fields have
   `allowed_values_status: "unresolved_open_decision"` (OD still open) - Gemini's
   response schema and the deterministic validator only apply a closed `enum`
   constraint where the eDAR source document actually defines one (currently just
   `injury_severity`). Fabricating enums for the rest to satisfy this phase would
   have meant inventing product decisions Phase 0 deliberately left open.

**Status:** Accepted.
