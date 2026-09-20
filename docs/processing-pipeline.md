# Processing Pipeline

## 1. Conceptual flow (source-specified)

```
Audio Input -> Audio Validation -> Audio Storage -> Speech-to-Text -> Transcript Validation
  -> Translation -> English Transcript Validation -> eDAR Extraction -> Schema Validation
  -> Provenance/Confidence -> Persist AI Result -> Officer Review -> Officer Approval
  -> Completed Record -> Export
```

## 2. Stage-by-stage contract

| Stage | Input | Output | Failure classification |
|---|---|---|---|
| Audio Validation | raw upload/recorded bytes | accept/reject (format, size, non-empty) | non-retryable (bad input) |
| Audio Storage | validated bytes | `Audio` row, `status: RECORDING/UPLOADED -> ` recording moves to `UPLOADED` | retryable (storage I/O) |
| Speech-to-Text | `Audio` | `Transcript(language=ORIGINAL)` | retryable (provider timeout/unavailable); non-retryable (unsupported language, corrupt audio) |
| Transcript Validation | original `Transcript` | pass/fail (non-empty, plausible language) | non-retryable |
| Translation | original `Transcript` | `Transcript(language=ENGLISH)` | retryable (provider); non-retryable (empty input) |
| English Transcript Validation | English `Transcript` | pass/fail | non-retryable |
| eDAR Extraction | English `Transcript` + `schemas/edar-schema.json` | raw structured extraction result | retryable (provider); non-retryable (malformed response after retries) |
| Schema Validation | raw extraction result | validated result or rejection | non-retryable — a schema-invalid AI response is a bug/prompt-drift signal, not a transient fault |
| Provenance/Confidence | validated result | each field tagged with confidence + source span (ADR-010) | — (part of the same stage as Schema Validation, not separately retryable) |
| Persist AI Result | tagged result | `EdarFieldValue` rows, `layer=AI`; recording -> `READY_FOR_REVIEW` | retryable (DB I/O) |
| Officer Review / Approval | officer action | `EdarFieldValue` rows, `layer=APPROVED`; `EdarRecord.review_status=APPROVED` | n/a — human action, not a pipeline stage |
| Export | approved record | export artifact | Phase 1+ (OD-006) |

Each pipeline stage (Audio Validation through Persist AI Result) is one `ProcessingJob`
(`job_type` groups Audio Validation+Storage under implicit recording-creation flow;
STT/Translation/Extraction are the three `ProcessingJob.job_type` values that actually run
asynchronously — see `docs/domain-model.md`). Validation sub-steps (Transcript Validation,
Schema Validation) are not separate jobs; they're synchronous checks inside the job that
produced the thing being validated, and a validation failure fails that job.

## 3. Asynchronous execution

Audio upload/validation/storage happens synchronously within the HTTP request (it's fast and
user-facing — the officer is waiting for upload confirmation). STT, translation, and
extraction do not: they are queued as `ProcessingJob` rows and executed by whatever
`TaskRunner` implementation is configured (ADR-009). The HTTP layer only ever creates a
`ProcessingJob` and returns immediately; it never blocks on provider calls.

```
csc_apps/processing/tasks/base.py     TaskRunner (ABC): enqueue(job) -> None
csc_apps/processing/tasks/inline.py   InlineTaskRunner: enqueue() executes the job synchronously,
                                       in-process - Phase 0 / local-dev only, never for production
                                       (no retry backoff, no isolation from the web process)
```

The production `TaskRunner` (Celery, RQ, cloud task queue, or otherwise) is
`docs/open-decisions.md` OD-003 — PMS has no precedent to follow here (`docs/
pms-reference-analysis.md` §1), so Phase 0 deliberately ships only the interface plus a
dev-mode implementation rather than guessing a production technology.

## 4. Provider isolation

Each external-AI stage talks to the pipeline only through an interface in
`csc_apps/processing/providers/`:

```
SpeechToTextProvider.transcribe(audio: Audio) -> TranscriptResult
TranslationProvider.translate(text: str, source_language: str | None) -> TranslationResult
ExtractionProvider.extract(english_text: str, schema: EdarSchema) -> ExtractionResult
```

No provider-specific SDK, API key, or request/response shape appears outside
`csc_apps/processing/providers/<provider_name>/` (empty in Phase 0 — no concrete provider is
implemented yet, per `docs/product-scope.md` §4). The business/pipeline layer imports only
the interface. See `docs/ai-extraction-contract.md` for the extraction contract specifically.

## 5. What Phase 0 implements vs. documents

Implemented: the `TaskRunner`/`InlineTaskRunner` abstraction, the three provider interfaces
(as Python `Protocol`/ABC with no concrete provider behind them), the `ProcessingJob`/
`ProcessingEvent` models. Documented only: the actual stage orchestration (what calls what,
in what order, wired to real providers) — that requires providers to exist, which is
Phase 1.
