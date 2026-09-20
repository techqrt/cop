# Phase 2 — Sarvam Speech-to-Text

## 1. Scope

```
Stored Audio -> Sarvam API -> Original-language Transcript -> Persist Transcript
  -> Expose Transcript through authenticated + authorized API
```

Phase 2 consumes the audio Phase 1 already stored and produces exactly one artifact:
an original-language `Transcript` row, retrievable through a new authenticated,
authorized endpoint. It does **not** implement live/streaming transcription,
translation, eDAR extraction, officer review/approval, or export — those remain later
phases (`docs/product-scope.md` §4).

## 2. Architecture

```
GET /recordings/<id>/                    (new, Phase 2)
  RecordingViewController.get -> RecordingView.get_extract
    resource-level authorization against the Recording row
    latest STT ProcessingJob's status -> response
    if SUCCEEDED: Transcript(language=ORIGINAL) -> response

python manage.py process_pending_stt_jobs  (new, Phase 2 - run by cron/manually)
  for each ProcessingJob(job_type='STT', status in PENDING/RETRYING):
    csc_apps.processing.stt_service.run_stt_job(job_id)
      1. status -> RUNNING, attempt_count += 1, STT_STARTED event
      2. Audio retrieved via AudioStorage.open() -> written to a temp file
      3. SpeechToTextProvider.transcribe(local_audio_path, content_type)
           -> SarvamSpeechToTextProvider (csc_apps/processing/providers/sarvam/)
      4. on success: Transcript.objects.update_or_create(...), status -> SUCCEEDED,
         STT_SUCCEEDED event, ActivityLog
      5. on failure: classify via error_classification.is_retryable(), status ->
         RETRYING or FAILED, STT_FAILED event
```

Never wired into the Phase 1 upload request — STT runs strictly outside that request/
response cycle (`docs/phase1-audio-ingestion.md` §15, source instructions §15).

## 3. Sarvam integration

**Verified against https://docs.sarvam.ai in 2026-09**, not assumed from memory —
this materially changed the design from what the task brief sketched:

- Sarvam's synchronous `POST /speech-to-text` REST endpoint caps audio at **30
  seconds**. Phase 1 accepts uploads well beyond that (crash-scene statements run
  minutes, and Phase 1's own size limit reasoning cites a 10-minute recording). The
  sync endpoint cannot serve this product.
- Sarvam's **Batch Speech-to-Text API** (`POST /speech-to-text/job/v1` + upload/start/
  status/download steps) supports files up to 2 hours and is the only documented fit.
  This is what Phase 2 integrates against.
- The Batch API's raw "start job" REST endpoint is **not published** in Sarvam's plain
  HTTP API reference — only exposed through the official `sarvamai` Python SDK
  (`job.start()`). Reimplementing it via raw `requests` calls would mean guessing an
  unpublished contract, which the source instructions explicitly forbid. This is why
  Phase 2 depends on the `sarvamai` SDK (`requirements.txt`) rather than doing a
  hand-rolled HTTP integration — see §12.
- Every endpoint, parameter, and response field this integration relies on was
  confirmed either from Sarvam's published docs or, where the docs were incomplete
  (the exact "start" contract, the SDK's internal exception model), from reading the
  installed `sarvamai` package's source directly (`ApiError(status_code, body,
  headers)` — a single flat exception class, not per-status subclasses).

### Request shape (via the SDK)

```python
job = client.speech_to_text_job.create_job(
    model='saaras:v3',
    mode='transcribe',       # hardcoded - never 'translate' (no translation in Phase 2)
    with_diarization=False,  # ADR-005 - one recording, one speaker
    with_timestamps=False,   # not needed for a plain-text transcript
    language_code=None,      # let Sarvam auto-detect ("unknown")
)
job.upload_files(file_paths=[local_audio_path])
job.start()
status = job.wait_until_complete(poll_interval=..., timeout=...)
```

### Response shape (downloaded output JSON, per file)

```json
{
  "request_id": "...",
  "transcript": "...",
  "language_code": "hi-IN",
  "timestamps": {"...": "..."},
  "diarized_transcript": {"...": "..."}
}
```

Only `transcript`, `language_code`, and `request_id` are read — `timestamps`/
`diarized_transcript` are ignored (Phase 2 doesn't need them and diarization is
explicitly out of scope).

### Authentication

Header `api-subscription-key: <SARVAM_API_KEY>`, set by the SDK from the
`api_subscription_key` constructor argument — always passed explicitly from
`Configurations.sarvam_api_key`, never left to the SDK's own `os.getenv
('SARVAM_API_KEY')` fallback, so configuration flows through this project's existing
`python-decouple`/`Configurations` convention only (`docs/pms-reference-analysis.md`
§1).

### A documented SDK quirk

The installed SDK's synchronous `SpeechToTextJob.upload_files()` sends a hardcoded
`Content-Type: audio/wav` header on the presigned-URL PUT regardless of the real
file's format. The actual filename (with its real extension) is what's declared to
`get_upload_links()` and is what Sarvam's backend appears to key off for processing —
this hasn't caused an observed failure, but it's recorded here because it's exactly
the kind of provider-specific detail worth being explicit about rather than silently
working around inside application code.

## 4. Provider abstraction

`csc_apps/processing/providers/base.py` (Phase 0 placeholder, completed here):
- `SpeechToTextProvider.transcribe(local_audio_path: str, content_type: str) ->
  TranscriptResult` — **signature changed** from Phase 0's `audio_storage_path: str`.
  Phase 0's placeholder implied the provider would resolve an opaque storage key
  itself, which couples it to a storage mechanism (source instructions §11
  explicitly forbid this). Phase 2 makes retrieval the *service's* job: the service
  pulls bytes from `AudioStorage`, writes a local temp copy, and only that local path
  is handed to the provider.
- `ProviderError(error_code, message)` (new) — the one exception type every provider
  implementation raises; `error_code` is looked up in
  `csc_apps.processing.error_classification`, never decided by the provider itself.

`csc_apps/processing/providers/sarvam/provider.py` implements
`SarvamSpeechToTextProvider(SpeechToTextProvider)`. No Sarvam type, response shape, or
SDK exception crosses this module's boundary — `csc_apps.processing.stt_service` (and
everything above it) only ever sees `TranscriptResult`/`ProviderError`.

## 5. Processing lifecycle

Reuses Phase 0's `ProcessingJob` model unchanged (no new fields, no new model) via its
existing `attempt_count`/`max_attempts`/`is_retryable`/`error_code`/`error_message`
columns:

```
PENDING --(run_stt_job)--> RUNNING --success--> SUCCEEDED
                              |
                              +--failure, retryable, attempts remain--> RETRYING
                              |         (picked up again by the next command run)
                              +--failure, non-retryable OR attempts exhausted--> FAILED
```

**Note on `docs/error-retry-strategy.md`'s two retry tiers (ADR-016):** that document
actually describes two distinct mechanisms - (a) an in-job, budget-bounded retry on
the *same* `ProcessingJob` row (`RUNNING -> RETRYING -> RUNNING`, bounded by
`attempt_count`/`max_attempts`), and (b) a *Recording*-level retry, triggered after a
job has gone terminally `FAILED`, that creates a **new** `ProcessingJob` row and
cycles `Recording.status` through `FAILED -> RETRY -> PROCESSING`. Phase 2 implements
only tier (a) - `run_stt_job`'s `RETRYING` handling. Tier (b) (an officer/admin action
that starts a fresh attempt after a job has permanently failed) is **not implemented**
- see §16.

`Recording.status` is untouched by Phase 2 — it was already `PROCESSING` after Phase
1's upload (docs/recording-state-machine.md), and Phase 2 doesn't move it further
(the recording doesn't reach `READY_FOR_REVIEW` until eDAR extraction exists, which is
a later phase entirely).

## 6. Transcript persistence

Phase 0's `Transcript` model needed **zero schema changes** — its existing
`unique_together: (recording, language)` constraint is exactly the idempotency
guarantee retry-safety needs (§8). `Transcript.objects.update_or_create(recording=,
language='ORIGINAL', defaults={text, detected_language_code, provider_name,
provider_metadata})` either creates the one original-language row or updates it in
place; it is never possible to have two `ORIGINAL` transcripts for one recording.
`provider_metadata` stores only `{"model": "saaras:v3", "request_id": "..."}` — never
the full raw Sarvam payload (source instructions §19, §35).

## 7. API endpoint

`GET /recordings/<int:recording_id>/` — the route Phase 0's own
`docs/api-architecture.md` had already sketched (`/recordings/<id>/...`), not the
literal `GET /recordings/{recording_id}/` example from the task brief's §20, though
it resolves to the same thing. Combines processing status and transcript into one
response rather than Phase 0's originally-sketched separate `/processing/` and
`/transcripts/` sub-resources — simpler, and matches the brief's own §24/§26 combined
response example; recorded as a documented simplification, not silently changing
`docs/api-architecture.md` without saying so (see that document's update).

```json
{
  "status": true,
  "message": "Transcript retrieved successfully",
  "data": {
    "recordingId": 42,
    "processingStatus": "SUCCEEDED",
    "transcript": {"text": "namaste, gaadi accident ho gaya", "language": "hi-IN"},
    "failureReason": null
  }
}
```

`processingStatus` mirrors `ProcessingJob.status` verbatim (`PENDING`, `RUNNING`,
`SUCCEEDED`, `FAILED`, `RETRYING`). `transcript` is `null` until `SUCCEEDED` — never
fabricated while processing is incomplete. `failureReason` is `null` unless `FAILED`,
and even then exposes only the controlled `error_code` (e.g.
`"STT_UNSUPPORTED_INPUT"`), never the raw provider `error_message`, filesystem path,
or credential.

## 8. Authentication & authorization

Authentication: the existing `JWTAuthentication` (`docs/pms-reference-analysis.md`
§7) — no second mechanism was introduced.

Authorization (resource-level, checked against the actual `Recording` row, not
inferred from the URL): the recording's owning officer, or any `REVIEWER`/`ADMIN`,
may view it. Any other `OFFICER` gets the same `ValueError` -> 400 rejection as a
nonexistent recording ID (same two-message pattern PMS's own `LeadView` uses for
not-found vs. forbidden — `docs/pms-reference-analysis.md` §3 — not a new
convention). This resolves the "owning officer + REVIEWER/ADMIN" branch of
`docs/open-decisions.md` OD-001/OD-007's still-unresolved exact permission matrix for
*this one endpoint specifically*; the full matrix (e.g. can a Reviewer write to a
recording?) remains open.

## 9. Error handling

`csc_apps/processing/error_classification.py` gained six Sarvam-specific codes: three
retryable (reusing existing `STT_PROVIDER_TIMEOUT`/`STT_PROVIDER_UNAVAILABLE`, plus
new `STT_PROVIDER_RATE_LIMITED`), three non-retryable (reusing existing
`STT_UNSUPPORTED_INPUT`, plus new `STT_AUTHENTICATION_FAILED`,
`STT_MALFORMED_RESPONSE`, `STT_EMPTY_TRANSCRIPT`). The provider maps every failure
mode it can produce — Sarvam `ApiError` by HTTP status (403->auth, 429->rate limit,
500/503->unavailable, 400/422->unsupported input), raw `httpx.TimeoutException`/
`httpx.TransportError` (network failures below the SDK's own error handling, which
only wraps HTTP *responses*, not transport failures), the SDK's own `RuntimeError` on
a failed presigned-URL PUT/GET, our own poll-timeout `TimeoutError`, and malformed/
empty-transcript output — onto one of these codes. Every code the provider can raise
is registered in `error_classification`; an unregistered code raises loudly
(`ValueError`) rather than defaulting silently, unchanged from Phase 0.

## 10. Retry handling

See §5. `max_attempts` defaults to 3 (`ProcessingJob.max_attempts`, unchanged Phase 0
default — `docs/error-retry-strategy.md` §2, still OD-009). A `RETRYING` job is picked
up again by the next `process_pending_stt_jobs` invocation; a `FAILED` job (non-
retryable, or attempts exhausted) stays `FAILED` until a human/ops action resets it —
no such reset mechanism exists in Phase 2 (not required by the acceptance criteria;
flagged as a gap in §15).

## 11. Task execution

PMS has no background-worker precedent (`docs/pms-reference-analysis.md` §1) and
Phase 0 deliberately deferred a concrete queue technology (ADR-009, OD-003). Phase 2
does not introduce Celery/RQ/Dramatiq. Instead: `csc_apps/processing/stt_service.py`
exposes one plain, directly-testable function, `run_stt_job(job_id)`, and
`python manage.py process_pending_stt_jobs` runs it for every runnable job — a
management command meant to be invoked by an external scheduler (cron, a systemd
timer, or by hand). This is the "clean mechanism to execute an STT ProcessingJob"
the source instructions ask for without guessing at a queue technology Phase 0 never
chose. OD-003 remains open and unchanged.

## 12. Configuration

| Setting | Env var | Default |
|---|---|---|
| Sarvam API key | `SARVAM_API_KEY` | *(none — required)* |
| Sarvam HTTP request timeout | `SARVAM_HTTP_TIMEOUT_SECONDS` | 60 |
| Batch job poll interval | `SARVAM_POLL_INTERVAL_SECONDS` | 5 |
| Batch job poll ceiling | `SARVAM_POLL_TIMEOUT_SECONDS` | 600 (10 min) |

All read via `csc.config.Configurations`, matching the project's existing
configuration convention (`docs/pms-reference-analysis.md` §1) — no direct
`os.environ` access in business logic. `SARVAM_API_KEY` has no default; an unset key
fails the same way a real 403 from Sarvam would (`STT_AUTHENTICATION_FAILED`), so
there's exactly one failure path to test and operate against, not two.

## 13. Dependency rule

New dependency: `sarvamai==0.1.34` (official SDK), plus its own transitive closure
(`httpx`, `pydantic`, `pydantic_core`, `typing_extensions`, `typing-inspection`,
`annotated-types`, `anyio`, `certifi`, `h11`, `httpcore`, `idna`, `websockets`),
pinned flatly in `requirements.txt` per this project's existing convention
(`docs/pms-reference-analysis.md` §11). Justified per §3/§12 above: the Batch API's
"start job" contract isn't published outside this SDK, so a raw-`requests`
integration would require guessing it. `websockets` is a hard dependency of the SDK
package itself (for its realtime-streaming client) but is never imported or used by
`SarvamSpeechToTextProvider` — Phase 2 uses only `client.speech_to_text_job`.

## 14. Security

- `SARVAM_API_KEY` lives in `.env` only (git-ignored), read via `Configurations`,
  never returned by any API response, never logged (`SarvamSpeechToTextProvider`
  logs nothing that includes it; `csc_apps.processing.stt_service`'s log lines carry
  only job/recording IDs, error codes, retryability, and durations — never audio
  bytes, transcript text, or credentials).
- Raw audio stays private throughout: `stt_service` retrieves it via
  `AudioStorage.open()` (never a public URL) into a `tempfile.TemporaryDirectory()`
  that is deleted (`with` block) immediately after the provider call returns,
  success or failure.
- The transcript endpoint requires authentication and performs resource-level
  authorization (§8) — verified by `RecordingDetailAPITests` covering all five
  scenarios the source instructions' §23 lists explicitly.
- Sarvam's raw output JSON (`request_id`, full `timestamps`, `diarized_transcript`,
  etc.) never reaches the API response or any persisted row beyond the two harmless
  `provider_metadata` fields (§6).

## 15. Testing

`csc_apps/processing/tests.py` (`SarvamSpeechToTextProviderTests`,
`SttServiceTests`) and `csc_apps/recordings/tests.py`
(`RecordingDetailAPITests`) — 36 new tests, all passing alongside the 63 from Phase
0/1 (99 total). No real Sarvam API call is made anywhere in the suite; the `SarvamAI`
SDK client class is patched at its import site
(`csc_apps.processing.providers.sarvam.provider.SarvamAI`), and its `job.download_
outputs()` writes real (synthetic, tiny) JSON to a real temp directory so the file-
reading code path is genuinely exercised, not mocked away.

Covers (mapped to the source instructions' §39 checklist): successful/malformed/
empty-transcript/auth-failure/rate-limit/timeout/5xx/network-failure provider
responses and their mapping to canonical results or `ProviderError`; a PENDING job
executing end-to-end (audio retrieved, provider invoked, transcript persisted, job
succeeds, `ProcessingEvent`/`ActivityLog` created); retryable-vs-non-retryable
classification, that retry never duplicates a transcript, and that the canonical
stored audio is byte-identical before and after a run; and all five API authorization
scenarios plus PENDING/RUNNING/FAILED/SUCCEEDED response shapes and envelope
conformance. A live smoke test against a real Sarvam account, using a key supplied
directly by the project owner, is tracked separately from the automated suite (never
committed, never part of CI).

Test fixture: reuses Phase 1's existing synthetic `tiny_valid.wav` — no new fixture,
no real crash-scene audio.

## 16. Known limitations

- ADR-016's tier (b) - a Recording-level retry that creates a fresh `ProcessingJob`
  after the existing one has gone terminally `FAILED` - is not implemented. An
  operator wanting to force a retry after exhausting `max_attempts` would need to do
  so directly (e.g. a Django shell/admin action). Not required by the acceptance
  criteria; a reasonable Phase 3+ addition if it comes up in practice.
- `process_pending_stt_jobs` has no distributed lock — two overlapping invocations
  could both read the same `PENDING` job before either writes `RUNNING`, in principle
  double-processing it. Acceptable for Phase 2's single-process, cron-driven model;
  a real task queue (OD-003) would remove this class of race entirely.
- Long recordings (near the 2-hour Batch API ceiling) could still exceed
  `SARVAM_POLL_TIMEOUT_SECONDS` (default 600s) before Sarvam finishes processing,
  classified as a retryable `STT_PROVIDER_TIMEOUT` — the next command run picks it
  back up via `job.get_job()`-style re-attachment is **not** implemented (a timed-out
  attempt creates a brand-new Sarvam job on retry rather than re-polling the same
  one). Acceptable for expected crash-statement lengths (minutes, not hours); flagged
  for revisit if very long recordings become common.
- The SDK's sync `upload_files()` Content-Type quirk (§3) is recorded but not worked
  around, since it hasn't caused an observed failure.

## 17. Deferred to Phase 3

Translation of the `ORIGINAL` transcript into English, persisting an `ENGLISH`
`Transcript` row, and exposing it through an authenticated/authorized API — per
`docs/architecture-decisions.md` ADR-004 and the source instructions' explicit stop
condition. `docs/open-decisions.md` OD-002's translation-provider selection remains
open.
