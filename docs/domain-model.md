# Domain Model

Conceptual entities first, then the Phase 0 relational shape actually implemented in
`csc_apps/*/models/`. Phase 0 intentionally keeps this minimal — see
`docs/product-scope.md` §4 for what's deferred.

## 1. Conceptual entities

| Entity | What it represents |
|---|---|
| User | An authenticated person: Officer, Reviewer, or Admin (`docs/security-baseline.md`). |
| Recording | One crash-report capture session: the container for an Audio, its Transcripts, its ProcessingJobs, and its EdarRecord. Owns the lifecycle state (`docs/recording-state-machine.md`) and the device-captured GPS. |
| Audio | A raw evidence file (Layer 1) — immutable once stored (ADR-002). One `ORIGINAL` per Recording plus zero or more `SUPPLEMENTAL` follow-ups (ADR-024, Phase 10B). |
| Transcript | Text produced from one Audio: `language = ORIGINAL \| ENGLISH`. The English one is always derived from the original via translation (ADR-004), never transcribed independently. Each Audio (original or supplemental) has its own pair. |
| ProcessingJob | One asynchronous unit of pipeline work (`job_type = STT \| TRANSLATION \| EXTRACTION \| EXPORT`) for one Audio, with retry/failure state (`docs/error-retry-strategy.md`). |
| ProcessingEvent | An immutable timeline entry for a Recording's pipeline (`docs/observability.md`) — distinct from PMS's CRUD-oriented `ActivityLog`. |
| EdarRecord | The header row for one Recording's eDAR data: one-to-one with Recording, carries only the record-level review/approval state. |
| EdarFieldValue | One value for one eDAR field, one layer (`AI` or `APPROVED`) — see ADR-011. This is where all 42 fields, and every Vehicle/Casualty repetition, actually live. |
| Export | (Named, not modeled in Phase 0 — see `docs/open-decisions.md` OD-006.) |

## 2. Why there is no `Vehicle` / `Casualty` table

The eDAR brief requires Vehicle (max 3, per Module D) and Casualty (unbounded, per Module E)
to be proper repeating groups, not flattened fixed columns
(`vehicle1_type`/`vehicle2_type`/...) — this is a direct instruction, not a style
preference. ADR-011 satisfies it *without* a dedicated `Vehicle`/`Casualty` table by
namespacing `EdarFieldValue.field_key`:

```
crash_date                                # Module A - record-level field
vehicle.1.vehicle_type                    # Module D - vehicle index 1
vehicle.1.registration_number
vehicle.2.vehicle_type                    # Module D - vehicle index 2
casualty.1.person_type                    # Module E - casualty index 1
casualty.1.injury_severity
casualty.2.person_type                    # Module E - casualty index 2
```

`field_key` is validated against `schemas/edar-schema.json` at write time: the base field
(`vehicle_type`, `injury_severity`, ...) must exist in the schema's `vehicle` or `casualty`
module, and the index must respect `schemas/edar-schema.json`'s `max_repetitions` (3 for
vehicle, unbounded for casualty). This *is* a proper relational, repeatable structure (one
row per field-instance, queryable and joinable) — it just doesn't require a schema migration
every time the eDAR field list changes, which a fixed-column table would.

If a later phase needs vehicle/casualty as first-class queryable rows (e.g. for a
"vehicles involved" report across many recordings), promoting them to real tables is a
additive migration, not a redesign — noted here so the tradeoff is visible, not hidden.

## 3. Phase 0 relational shape

```
User (csc_apps/authentication)
  user_id PK, email (unique), password_hash, name, role [OFFICER|REVIEWER|ADMIN],
  access_token, refresh_token, is_active, created_at, updated_at

Recording (csc_apps/recordings)
  recording_id PK, officer      FK -> User (PROTECT)
  status            [see docs/recording-state-machine.md]
  gps_latitude, gps_longitude, gps_captured_at        (device-captured, ADR-012)
  road_name, police_station_jurisdiction, case_fir_number   (Module A fields that are
                                                              record-identity, not AI output -
                                                              see note below)
  created_at, updated_at

Audio (csc_apps/recordings)
  audio_id PK, recording FK -> Recording (PROTECT)
  role              [ORIGINAL|SUPPLEMENTAL]  (default ORIGINAL)
  source            [LIVE|UPLOAD]
  storage_path, content_type, original_filename, duration_seconds, file_size_bytes,
  checksum_sha256 (indexed - Phase 1 duplicate-submission lookup, docs/open-decisions.md
  OD-011), uploaded_at
  # original_filename added in Phase 1 (docs/phase1-audio-ingestion.md §5) - display/
  # audit metadata only, never used to build storage_path.
  # `recording` changed from a one-to-one FK to a plain FK, and `role` was added, in
  # Phase 10B (docs/phase10b-supplemental-audio.md, ADR-024): a Recording now has
  # exactly one ORIGINAL Audio (unchanged identity/immutability, ADR-002) plus zero
  # or more SUPPLEMENTAL ones, each a later targeted follow-up recording used only
  # to fill in eDAR fields the original left UNKNOWN.

Transcript (csc_apps/recordings)
  transcript_id PK, recording FK -> Recording (PROTECT), audio FK -> Audio (nullable, PROTECT)
  language          [ORIGINAL|ENGLISH]
  text, detected_language_code (the language *this row's text* is written in - see
  note below), provider_name, provider_metadata (JSON)
  created_at
  unique_together: (audio, language)
  # Phase 2 (docs/phase2-sarvam-stt.md §6): ORIGINAL rows are actively written by
  # csc_apps.processing.stt_service. Phase 3 (docs/phase3-sarvam-translation.md §9):
  # ENGLISH rows are actively written by csc_apps.processing.translation_service.
  # Zero schema changes were needed for either phase - the unique_together
  # constraint already envisioned here is exactly both phases' retry-safety
  # guarantee (ADR-004). Note: an ENGLISH row's detected_language_code is "en-IN"
  # (the language of its own text), not the language it was translated from - that
  # provenance lives in provider_metadata.source_language_code instead.
  # Phase 10B (docs/phase10b-supplemental-audio.md, ADR-024): `audio` was added and
  # the uniqueness constraint moved from (recording, language) to (audio, language)
  # - each Audio (original or supplemental) now gets its own independent ORIGINAL/
  # ENGLISH transcript pair, rather than every Audio on a Recording colliding on
  # one shared pair. Nullable only for migration simplicity; every row written since
  # Phase 10B sets it.

ProcessingJob (csc_apps/processing)
  job_id PK, recording FK -> Recording (PROTECT), audio FK -> Audio (nullable, PROTECT)
  job_type          [STT|TRANSLATION|EXTRACTION|EXPORT]
  status            [PENDING|RUNNING|SUCCEEDED|FAILED|RETRYING]
  attempt_count, max_attempts
  is_retryable, error_code, error_message           (docs/error-retry-strategy.md)
  provider_name, provider_metadata (JSON)
  started_at, completed_at, created_at
  # `audio` added in Phase 10B (docs/phase10b-supplemental-audio.md, ADR-024) - null
  # only for EXPORT jobs; every STT/TRANSLATION/EXTRACTION job sets it, and it is
  # what lets one Recording have more than one independent job chain (one per
  # Audio). csc_apps.processing.extraction_service dispatches a full-replace vs.
  # merge-only extraction based on job.audio.role, not a second job_type.

ProcessingEvent (csc_apps/processing)
  event_id PK, recording FK -> Recording (PROTECT), job FK -> ProcessingJob (nullable, PROTECT)
  event_type        (free-form but conventionally "<stage>_<started|completed|failed>",
                     e.g. "stt_started" - see docs/observability.md)
  metadata (JSON), occurred_at

EdarRecord (csc_apps/edar)
  edar_record_id PK, recording FK -> Recording (PROTECT, one-to-one)
  review_status     [PENDING_REVIEW|IN_REVIEW|APPROVED]
  reviewed_by FK -> User (nullable), reviewed_at (nullable)
  # Phase 5 (docs/phase5-validation-provenance.md) - AI-candidate provenance & quality.
  # Describe the CURRENT AI candidate only; none of it implies approval.
  quality_status    [VALIDATED|VALIDATION_WARNING|null]   (INVALID candidates are never stored)
  quality_report (JSON)          structured warnings + count metrics, no accuracy score
  source_transcript FK -> Transcript (nullable, PROTECT)  the ENGLISH transcript extracted from
  extraction_job FK -> ProcessingJob (nullable, PROTECT)  the job that produced the candidate
  extracted_at (nullable)

EdarFieldValue (csc_apps/edar)
  field_value_id PK, edar_record FK -> EdarRecord (PROTECT)
  field_key          (e.g. "crash_date", "vehicle.1.vehicle_type" - validated against schema)
  layer              [AI|APPROVED]                                  (ADR-008)
  known              [KNOWN|UNKNOWN|NOT_APPLICABLE|UNCERTAIN]        (docs/unknown-data-policy.md)
  value (JSON)                                                       (null when not KNOWN)
  confidence (float, nullable - AI layer only)                       (ADR-010)
  source_transcript_segment (text, nullable)
  source_start_time, source_end_time (float seconds, nullable)
  extraction_version (string, nullable - AI layer only)
  updated_by FK -> User (nullable - APPROVED layer only), updated_at
  unique_together: (edar_record, field_key, layer)
```

# Phase 4 (docs/phase4-gemini-edar-extraction.md): both models are now actively
# written by csc_apps.processing.extraction_service, unchanged from this Phase 0
# design - zero schema changes were needed. `layer='AI'` is written on every
# extraction; `layer='APPROVED'` remains a real, unused enum value, waiting for a
# later officer-approval phase. `known` currently only ever takes `KNOWN`/`UNKNOWN`
# in practice (Gemini-based extraction doesn't attempt to distinguish `UNCERTAIN`
# or auto-set `NOT_APPLICABLE` - docs/phase4-gemini-edar-extraction.md §13,
# docs/unknown-data-policy.md §5, still OD-005).

Note on Module A fields living on `Recording` rather than `EdarFieldValue`: `crash_date`,
`crash_time`, `road_name`, `police_station_jurisdiction`, and `case_fir_number` are
record-identity fields the officer confirms at creation/upload time, not primarily an
extraction target — GPS is device-captured (ADR-012) and jurisdiction is a resolver lookup
(ADR-014). They are still *listed* in `schemas/edar-schema.json` as Module A fields for
completeness and export purposes. As of Phase 4, the AI extraction pipeline *does* populate
an `EdarFieldValue` row for `crash_date`/`crash_time`/`road_name`/
`police_station_jurisdiction`/`case_fir_number` when the transcript supports them (as a
cross-check against the officer-confirmed `Recording` fields) — `gps_coordinates` remains
the one Module A field never sent to the extraction provider at all (ADR-012,
docs/phase4-gemini-edar-extraction.md §7).

## 4. Explicitly deferred (not modeled in Phase 0)

- `Export` — no table; export format/destination is OD-006.
- `Review` as a distinct entity — review state lives on `EdarRecord.review_status` plus the
  `ProcessingEvent` timeline; a dedicated review-comments/audit table is Phase 1 if needed.
- Any UI-facing read model (denormalized "get eDAR record as one JSON blob" view) — Phase 1,
  built by aggregating `EdarFieldValue` rows for one `EdarRecord`.
