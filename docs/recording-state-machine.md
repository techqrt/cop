# Recording State Machine

## 1. States (source-specified lifecycle)

```
CREATED -> RECORDING -> UPLOADED -> PROCESSING -> READY_FOR_REVIEW -> IN_REVIEW -> COMPLETED

Failure path:
PROCESSING -> FAILED -> RETRY -> PROCESSING
```

This is the exact lifecycle given in the brief. `Recording.status` is a plain string field
with these values as `choices`, following PMS's convention of plain tuple choices rather
than a separate state-table (`docs/pms-reference-analysis.md` §4).

## 2. State meanings

| State | Meaning |
|---|---|
| `CREATED` | Recording session started (GPS captured, officer identified); no audio yet. |
| `RECORDING` | Live audio capture in progress. Skipped entirely for the upload path — an uploaded `Recording` goes `CREATED` -> `UPLOADED` directly. |
| `UPLOADED` | An `Audio` row exists and its bytes are stored; the recording is ready for the pipeline. Both input paths (live-recording-finished, or file-upload-finished) land here — this is the convergence point from ADR-003. |
| `PROCESSING` | At least one `ProcessingJob` (STT, translation, or extraction) is running or pending for this recording. |
| `READY_FOR_REVIEW` | All three pipeline stages succeeded; an `EdarRecord` with `AI`-layer `EdarFieldValue` rows exists. |
| `IN_REVIEW` | An officer/reviewer has opened the record for review (distinct from "ready" so two reviewers don't collide — see `docs/open-decisions.md` OD-001 for the exact review-assignment model). |
| `COMPLETED` | `EdarRecord.review_status = APPROVED`; the `APPROVED`-layer `EdarFieldValue` rows are final. |
| `FAILED` | A `ProcessingJob` exhausted its retries with a non-retryable or retry-exhausted error (`docs/error-retry-strategy.md`). |
| `RETRY` | A manual or automatic retry has been triggered from `FAILED`; transitions immediately back to `PROCESSING` once the new `ProcessingJob` attempt starts. |

## 3. Legal transitions (Phase 0 contract)

```
CREATED           -> RECORDING | UPLOADED
RECORDING         -> UPLOADED
UPLOADED          -> PROCESSING
PROCESSING        -> READY_FOR_REVIEW | FAILED
READY_FOR_REVIEW  -> IN_REVIEW
IN_REVIEW         -> COMPLETED | READY_FOR_REVIEW   (kicked back if the reviewer isn't done)
FAILED            -> RETRY
RETRY             -> PROCESSING
COMPLETED         -> (terminal)
```

No other transition is valid. This table is implemented as data (not scattered `if`
statements) in `csc_apps/recordings/state_machine.py::ALLOWED_TRANSITIONS`, with a single
`can_transition(from_state, to_state) -> bool` / `transition(recording, to_state)` pair of
functions — mirroring PMS's preference for small, single-responsibility functions
(`docs/pms-reference-analysis.md` §12). Every transition additionally writes a
`ProcessingEvent` (`docs/observability.md`), so the full state history is reconstructable
without relying on `updated_at` alone.

## 4. What Phase 0 implements vs. documents

Phase 0 implements the transition table and its validation function, with unit tests
covering the legal and illegal transitions (`csc_apps/recordings/tests.py`). It does **not**
wire this into the actual pipeline (`csc_apps/processing`) triggering transitions
automatically — that requires the working STT/translation/extraction providers, which are
Phase 1 (`docs/product-scope.md` §4).
