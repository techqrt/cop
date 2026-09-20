# Observability

## 1. Two distinct event streams (deliberately not one)

PMS has a single audit stream, `ActivityLog` — one row per CRUD action, driven by a Django
signal plus request-scoped `threading.local()` context (`docs/pms-reference-analysis.md`
§9). CSC keeps that exact mechanism for CRUD auditing (`csc_apps/activity_log/`, ported
close to verbatim) but adds a **second**, separate stream for pipeline observability,
because the two have different shapes, different consumers, and different retention needs:

| | `ActivityLog` (ported from PMS) | `ProcessingEvent` (new) |
|---|---|---|
| Triggered by | HTTP request performing Create/Update/Delete on a tracked model | A recording's pipeline advancing a stage |
| Consumer | Security/compliance audit ("who changed what") | Engineering/support ("why is this recording stuck") + the officer's own progress view |
| Shape | `user, action, model, method, end_point, details, created_on` | `recording, job, event_type, metadata, occurred_at` |
| One row per | HTTP request | Pipeline milestone (may have zero HTTP requests behind it — background jobs) |

## 2. Recording lifecycle event timeline (source-specified)

```
Recording created
  -> Upload completed
  -> STT started -> STT completed
  -> Translation started -> Translation completed
  -> Extraction started -> Extraction completed
  -> Review started
  -> Completed
```

Each arrow is a `ProcessingEvent` row. `event_type` follows a `<stage>_<started|completed|
failed>` convention (`recording_created`, `upload_completed`, `stt_started`,
`stt_completed`, `stt_failed`, ...) so a recording's full history is one ordered query:
`ProcessingEvent.objects.filter(recording=r).order_by('occurred_at')`. `metadata` (JSON)
carries stage-specific detail (e.g. `{"provider": "...", "duration_ms": ...}` for a
completed STT event, or `{"error_code": ..., "is_retryable": ...}` for a failed one) without
needing a different column set per event type.

## 3. What must be logged

- **Application logs** — standard Django/Python logging, following whatever log level PMS
  uses by default (PMS does not configure a custom `LOGGING` dict in `settings.py`, so it
  relies on Django's default console logging — CSC starts from the same default rather than
  introducing a new logging framework unasked; a structured-logging upgrade is an open
  decision if Phase 1 needs it, not assumed here).
- **Processing logs** — every `ProcessingEvent`, persisted (not just emitted to console),
  since it's the mechanism the officer's UI and support tooling both read from.
- **Errors** — every `ProcessingJob` failure is both a `ProcessingEvent`
  (`event_type=<stage>_failed`) and updates `ProcessingJob.error_code`/`error_message`
  directly, so a failure is queryable from either the job or the timeline.
- **Job status** — `ProcessingJob.status` is always current; polled, not pushed, in Phase 0
  (no websocket/notification mechanism designed yet).
- **Processing duration** — derivable from `ProcessingJob.started_at`/`completed_at`, and
  independently from consecutive `ProcessingEvent.occurred_at` timestamps for the same
  stage (kept as two ways to get the same number deliberately — one on the job row for fast
  querying, one on the timeline for a human-readable history).
- **AI provider metadata** — `provider_name` + `provider_metadata` (JSON) on both
  `ProcessingJob` and `Transcript`, e.g. provider request ID, model version — needed to
  answer "which provider/version produced this value" months later, tying back into
  `extraction_version` in `docs/data-provenance.md`.
- **Audit events** — `ActivityLog`, as above, for who-changed-what on any tracked model
  (initially: `EdarFieldValue` APPROVED-layer writes, `EdarRecord.review_status` changes).

## 4. What Phase 0 implements vs. documents

Implemented: `ProcessingEvent` model, `ActivityLog` model + middleware (ported from PMS),
the `event_type` naming convention as a documented contract. Phase 1 added the first
concrete named constants (`csc_apps/processing/event_types.py`:
`RECORDING_CREATED`, `AUDIO_VALIDATED`, `AUDIO_STORED`, `PROCESSING_JOB_CREATED` —
`docs/phase1-audio-ingestion.md` §2) plus the generic
`recording_transitioned_<from>_to_<to>` events already emitted by
`csc_apps.recordings.state_machine.transition()`. Phase 2 added
`STT_STARTED`/`STT_SUCCEEDED`/`STT_FAILED`; Phase 3 added
`TRANSLATION_JOB_CREATED`/`TRANSLATION_STARTED`/`TRANSLATION_SUCCEEDED`/
`TRANSLATION_FAILED`; Phase 4 added `EXTRACTION_JOB_CREATED`/`EXTRACTION_STARTED`/
`EXTRACTION_SUCCEEDED`/`EXTRACTION_FAILED` (`docs/phase4-gemini-edar-extraction.md`
§11); Phase 5 added `QUALITY_VALIDATION_SUCCEEDED`/`QUALITY_VALIDATION_FAILED`, emitted inside the extraction stage before its succeeded/failed event (`docs/phase5-validation-provenance.md` §11) - metadata carries counts and status only, never field values or evidence. The three pipeline stages now cover the full `Audio -> STT -> Translation ->
Extraction` chain end to end, each with its own started/succeeded/failed events plus
one `<stage>_job_created` event marking automatic chaining from the previous stage's
success. Still not enforced by a Django `choices` field — even with all three AI
stages now present, widening `event_type` to a closed set remains a low-priority
tightening, not a blocking gap. Documented only: dashboards/alerting on top of
these streams — no requirement calls for them yet.
