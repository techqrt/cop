# Phase 1 — Backend Audio Upload & Ingestion

## 1. Scope

Phase 1 implements exactly the ingestion slice of the pipeline:

```
Client Audio File -> Django API -> Audio Validation -> Audio Storage
  -> Recording persistence -> ProcessingJob creation -> Processing state management
```

It does **not** implement: microphone recording, live/streaming transcription, the
Sarvam STT integration, translation, eDAR extraction, officer review/approval, or
export. Those are Phase 2+ (`docs/product-scope.md` §4, `docs/processing-pipeline.md`).
A `ProcessingJob(job_type='STT', status='PENDING')` row is created as the placeholder
for future AI work; nothing consumes it in this phase.

## 2. Architecture

```
POST /recordings/  (multipart/form-data, Bearer token required)
  csc_apps.recordings.controller.RecordingViewController.upload
    @extend_schema + @api_view(['POST']) + SerializerValidations(...).validate
  -> csc_apps.recordings.views.RecordingView.upload_extract
    @Common(response_handler=...).exception_handler
    1. validate_audio_upload()          - csc_apps.recordings.validators, no DB writes
    2. transaction.atomic():
       a. Recording.objects.create(status='CREATED', ...)
       b. ProcessingEvent: RECORDING_CREATED, AUDIO_VALIDATED
       c. storage.save(...)              - csc_apps.recordings.storage (not transactional)
       d. Audio.objects.create(...)
       e. ProcessingEvent: AUDIO_STORED
       f. transition(recording, 'UPLOADED')
       g. ProcessingJob.objects.create(job_type='STT', status='PENDING')
       h. ProcessingEvent: PROCESSING_JOB_CREATED
       i. transition(recording, 'PROCESSING')
       j. ActivityLog.record(...)
       k. duplicate-submission check (read-only)
  -> Response: {status, message, data: {recordingId, status, audioId,
       processingJobId, possibleDuplicateOfRecordingId}}
```

Layering, the response envelope, and the exception-handling decorator are unchanged
from Phase 0 / PMS (`docs/pms-reference-analysis.md` §§3, 6, 8) - this endpoint adds no
new cross-cutting mechanism.

## 3. API endpoint

`POST /recordings/` — `multipart/form-data`, requires `Authorization: Bearer <token>`.
Any authenticated role (`OFFICER`, `REVIEWER`, `ADMIN`) may call it — the exact
per-role permission matrix is still `docs/open-decisions.md` OD-007; Phase 1's interim
default is "any authenticated user may ingest audio," not a new restriction.

| Field | Required | Type | Notes |
|---|---|---|---|
| `audio` | yes | file | See §4 for accepted formats/size. |
| `gps_latitude` | no | float | Device-captured (ADR-012), passed through as-is. |
| `gps_longitude` | no | float | |
| `road_name` | no | string | Module A context (`docs/domain-model.md` §3). |
| `police_station_jurisdiction` | no | string | Free text in Phase 1 — resolver mechanism is OD-004, unimplemented. |
| `case_fir_number` | no | string | |

201 response:

```json
{
  "status": true,
  "message": "Audio uploaded and queued for processing",
  "data": {
    "recordingId": 42,
    "status": "PROCESSING",
    "audioId": 17,
    "processingJobId": 9,
    "possibleDuplicateOfRecordingId": null
  }
}
```

No audio URL, storage path, or credential ever appears in the response (§7). 400
responses use the standard `{status: false, message, error: [...]}` shape
(`docs/pms-reference-analysis.md` §6).

No retrieval/download endpoint is added in Phase 1 — deferred, not overlooked
(`docs/open-decisions.md` OD-012).

## 4. Validation (`csc_apps/recordings/validators.py`)

Checked in order, each raising `ValueError` (400) on failure, before any database
write:

1. Non-empty (`size > 0`).
2. `size <= settings.AUDIO_MAX_UPLOAD_SIZE_BYTES` (interim default 100 MB, OD-010).
3. Declared `Content-Type` is in the allowlist (`.wav`, `.mp3`, `.m4a`, `.aac`, `.ogg`,
   `.webm`, `.flac` — OD-010).
4. The file's first bytes match a known signature for that format (RIFF/WAVE, ID3/MPEG
   frame sync, `ftyp`, `OggS`, EBML, `fLaC`) — the client-declared content-type is
   **never** trusted on its own.

The extension used to build the storage key always comes from the validated
content-type mapping, never from the client-supplied filename — the filename is kept
only as `Audio.original_filename` display/audit metadata (`os.path.basename`, truncated
to 255 chars), and never touches a filesystem path.

## 5. Storage architecture (`csc_apps/recordings/storage/`)

```
AudioStorage (ABC)          - save() / delete() / open(), csc_apps/recordings/storage/base.py
LocalPrivateAudioStorage    - the only implementation, csc_apps/recordings/storage/local.py
```

- Writes under `settings.PRIVATE_STORAGE_ROOT` (default `<repo>/private_storage/`,
  configurable via `PRIVATE_STORAGE_ROOT`) — a directory that is **never** mounted
  under `MEDIA_URL`/`STATIC_URL` and never reachable through Django's `static()`
  dev-serving helper. This satisfies "private by default" (`docs/security-baseline.md`
  §3) without any cloud credentials.
- Storage key: `recordings/{recording_id}/audio/{uuid4}{extension}` — collision-
  resistant, server-generated, never derived from client input.
- Writes go to a `.part` temp file first, then `os.replace()` (atomic within one
  filesystem) into the final path; a write failure cleans up the temp file and raises
  `AudioStorageError`. No reader ever observes a partially-written file.
- SHA-256 checksum is computed while streaming the write (no second read pass) and
  persisted on `Audio.checksum_sha256` — evidentiary integrity, **not** used for
  authorization (per the source instructions).
- The database transaction boundary does **not** cover storage I/O (it can't — it's not
  a database operation). See §6 for exactly how this is handled.
- Swapping in a cloud backend later (OD-008, still open) means implementing
  `AudioStorage` again and changing `csc_apps.recordings.storage.get_storage()` — no
  call site, model, or migration changes.

## 6. Transaction and storage-failure handling

The ingestion view wraps `Recording`/`Audio`/`ProcessingJob`/`ProcessingEvent`/
`ActivityLog` writes in one `transaction.atomic()` block, with `storage.save()`
called partway through it:

- **Validation fails** → no DB writes, no storage writes. Nothing to clean up.
- **`storage.save()` fails** → nothing was written to disk (`LocalPrivateAudioStorage`
  guarantees this internally); the `Recording` row(s) already inserted in this
  transaction roll back when the exception propagates out of the `atomic()` block.
  Result: no orphan DB row, no orphan file.
- **A DB write *after* `storage.save()` succeeds fails** (e.g. `Audio.objects.create`
  raising) → caught by an inner `except Exception: storage.delete(stored.storage_path);
  raise`, which best-effort deletes the just-written file before re-raising, which then
  rolls back the transaction. **Documented boundary:** this delete can itself fail
  (e.g. the same disk fault that caused the original failure) — an orphaned *file*
  with no matching DB row is the accepted residual risk of a non-transactional storage
  backend. A periodic orphan-file sweep is a reasonable Phase 2+ addition, not built
  here (avoids overbuilding for a residual, low-probability failure mode).

Tested: `test_storage_failure_creates_no_database_records`,
`test_database_failure_after_storage_success_deletes_stored_file_and_rolls_back`
(`csc_apps/recordings/tests.py`).

## 7. Recording lifecycle

```
CREATED -> UPLOADED -> PROCESSING
```

Matches `docs/recording-state-machine.md`'s existing transition table unchanged — no
new states were added. `PROCESSING` (rather than stopping at `UPLOADED`) is used
because Phase 0's own definition of `PROCESSING` is "at least one ProcessingJob...
running **or pending**" (`docs/recording-state-machine.md` §2), and a `PENDING` STT job
now exists the moment ingestion succeeds. This is not a claim that transcription has
started — it hasn't, and won't until Phase 2. The recording never reaches
`READY_FOR_REVIEW` in this phase.

## 8. ProcessingJob behavior

One `ProcessingJob(job_type='STT', status='PENDING')` is created per successful
ingestion. It is **not** enqueued on a `TaskRunner` (`docs/processing-pipeline.md`
§3) — there is no STT provider yet to run, so there is nothing to execute; creating
the row *is* the "scheduling" Phase 1 is responsible for. No Celery/RQ/Dramatiq or any
other queue technology was introduced — `docs/open-decisions.md` OD-003 remains open,
unchanged.

## 9. Error handling

Reuses `csc_apps.common.common.Common.exception_handler` unchanged
(`docs/pms-reference-analysis.md` §8): `ValueError` (validation failures, including all
of §4's checks and `AudioStorageError`, which is a plain exception falling through the
generic branch) → 400 with the standard envelope; authentication failure → 401 (from
`SerializerValidations`, before the view even runs). No filesystem path, stack trace, or
credential is ever included in a response — `Utils.env_exception_handler` redacts
exception detail outside `DEBUG` mode, unchanged from Phase 0.

## 10. Security

- Upload requires authentication (existing `JWTAuthentication`, unchanged — no second
  JWT implementation was introduced).
- Audio is private by construction (§5) — no public URL, ever.
- No provider credentials exist in this phase (none are called).
- Filenames never reach a filesystem path (§4).
- `ActivityLog.record()` logs the action/model/IDs, never audio bytes or file content.
- No transcript exists yet in Phase 1, so there is nothing transcript-shaped to
  accidentally log.

## 11. Configuration (new in Phase 1)

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `AUDIO_MAX_UPLOAD_SIZE_BYTES` | `AUDIO_MAX_UPLOAD_SIZE_BYTES` | 104857600 (100 MB) | OD-010, interim. |
| `PRIVATE_STORAGE_ROOT` | `PRIVATE_STORAGE_ROOT` | `<repo>/private_storage/` | OD-008 interim backend location. |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | derived | `AUDIO_MAX_UPLOAD_SIZE_BYTES + 1MB` | Not independently configurable — see the comment in `csc/settings.py`: it must stay above the application-level limit or Django rejects an oversized request before the JSON-enveloped validator ever runs. |

No new third-party dependency was added (`docs/pms-reference-analysis.md` §11,
`requirements.txt` unchanged) — the storage and validation layers use only the Python
standard library plus what Phase 0 already installed (Django/DRF).

## 12. Testing

`csc_apps/recordings/tests.py` (30 new tests, all passing alongside the 33 from Phase
0 — 63 total): `AudioUploadValidatorTests`, `LocalPrivateAudioStorageTests`,
`RecordingUploadAPITests`. Covers: authenticated/unauthenticated/any-role upload,
missing file, unsupported format, signature-mismatch, oversized file, all-records-created
happy path, response envelope on success/failure, recording state reached, the four
named `ProcessingEvent`s plus the state-transition events, `ActivityLog` creation,
private storage location, storage-failure rollback, post-storage DB-failure
compensation, sequential-upload non-corruption, and the duplicate-checksum signal
(both inside and outside its detection window). Fixture:
`csc_apps/recordings/tests_fixtures/tiny_valid.wav` — a synthetic ~0.1s silent WAV,
no real speech, no PII (per the source instructions' test-data rule).

## 13. Known limitations

- `LocalPrivateAudioStorage` is filesystem-only — fine for one server, not for a
  multi-instance deployment without shared/networked storage. OD-008 tracks the
  production backend.
- No download/retrieval endpoint exists — a future consumer (Phase 2's STT step) reads
  directly via `AudioStorage.open()` in-process, not over HTTP.
- Duplicate detection (§14) is a heuristic signal, not a hard dedup guarantee.
- `Audio.duration_seconds` is left `null` — computing it needs an audio-parsing
  dependency (mutagen/ffprobe), deliberately not added per the "no media-processing
  dependency in Phase 1" instruction.

## 14. Deferred / open decisions touched by Phase 1

New (this phase):

- **OD-010** — Audio upload size limit (100 MB) and content-type allowlist are interim
  defaults, not sourced from a product requirement; revisit with real device/provider
  data.
- **OD-011** — True request-retry idempotency (e.g. a client-supplied idempotency-key
  header deduplicated before any write) was not built. The safe minimum implemented
  instead: a same-officer, same-checksum upload within a 5-minute window is still
  ingested normally, but the response's `possibleDuplicateOfRecordingId` surfaces the
  likely-duplicate prior recording so a client can decide what to do with it.
- **OD-012** — An authenticated audio retrieval/download endpoint was not built in
  Phase 1 (§13); add one only when a real consumer needs it.

Unchanged (still open, not resolved or altered by this phase): OD-001 through OD-009
in `docs/open-decisions.md`, including OD-008 (production storage backend) and OD-003
(production task queue) — this phase implements *interfaces* consistent with both,
resolves neither.
