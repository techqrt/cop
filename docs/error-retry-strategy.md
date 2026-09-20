# Error / Retry Strategy

PMS has no retry concept anywhere (`docs/pms-reference-analysis.md` §8) — every PMS request
is synchronous and a failure is terminal, returned once to the caller. CSC's asynchronous
pipeline (ADR-009) needs one; this document is new, not adapted from PMS, though it reuses
PMS's "funnel through one place" instinct (one classifier, not scattered try/excepts).

## 1. Failure classification

| Failure | Retryable? | Why |
|---|---|---|
| Audio upload I/O error (storage backend timeout) | Yes | Transient infrastructure fault. |
| Invalid audio (corrupt file, empty, unsupported codec) | No | The input itself is bad; retrying produces the same failure. |
| STT provider timeout / provider unavailable (5xx) | Yes | Transient. |
| STT provider rejects the audio (unsupported language/format it validated after accepting) | No | Deterministic given this input. |
| Translation provider timeout / unavailable | Yes | Transient. |
| Translation provider returns empty output for non-empty input | No | Deterministic; needs a different input or provider, not a retry. |
| Extraction provider timeout / unavailable | Yes | Transient. |
| Extraction provider returns a malformed/schema-invalid response | No | A prompt/response-format problem, not a transient fault — retrying with identical input is expected to fail identically (`docs/ai-extraction-contract.md` §4). |
| Schema validation failure (all fields invalid) | No | Same reasoning as above. |
| Database write failure during persistence | Yes | Transient (connection blip, lock contention). |
| Export generation failure | Not modeled in Phase 0 | (OD-006) |

## 2. Retry mechanics

```
ProcessingJob.status: PENDING -> RUNNING -> SUCCEEDED
                                          -> FAILED (is_retryable=False, or attempt_count >= max_attempts)
                                          -> RETRYING -> RUNNING  (attempt_count += 1, is_retryable=True, attempt_count < max_attempts)
```

- `max_attempts` defaults to 3 for provider-call stages (STT, translation, extraction) — an
  **ARCHITECTURAL DECISION**, not sourced from the brief, chosen as a conservative default
  that Phase 1 can tune per provider once real failure-rate data exists.
  `docs/open-decisions.md` OD-009 tracks whether this should be provider-specific or
  configurable per job type.
- Backoff strategy (fixed delay vs. exponential) is **not specified in Phase 0** — it depends
  on which `TaskRunner` implementation is chosen (ADR-009, OD-003), since most queue
  technologies provide backoff natively rather than requiring CSC to implement it. The
  `ProcessingJob` model stores enough state (`attempt_count`, `max_attempts`) for any backoff
  policy to be layered on top without a schema change.
- On `FAILED`, the owning `Recording.status` moves to `FAILED` (`docs/
  recording-state-machine.md`). A manual retry (officer/admin action, or an automatic
  scheduled retry — not decided, OD-009) moves it to `RETRY` then back to `PROCESSING`,
  creating a **new** `ProcessingJob` row for that stage rather than reusing the failed one,
  so the failure history stays intact for observability.

## 3. Non-retryable failures reaching a human

A non-retryable failure does not just sit in `FAILED` silently — it must be visible on the
officer's Dashboard/History (`docs/user-workflow.md` §2) with enough detail (`error_code`,
human-readable `error_message`) that the officer knows whether the fix is "try uploading
again" (bad audio) or "wait/retry" (a manual retry might succeed once a transient provider
issue clears, even though the system itself classified it as non-retryable at the time —
the classification governs *automatic* retry, not officer choice to retry manually). This
distinction is worth stating explicitly since retryable/non-retryable easily gets conflated
with "can never be retried" — it should read as "will the system retry this for you
automatically."

## 4. What Phase 0 implements vs. documents

Implemented: `ProcessingJob` model with the full status/attempt/retryable field set, and the
failure-classification table above encoded as a lookup
(`csc_apps/processing/error_classification.py`) with unit tests for the retryable/
non-retryable split. Documented only: actual backoff scheduling and the automatic-retry
trigger — both depend on the `TaskRunner` implementation chosen in Phase 1 (OD-003).
