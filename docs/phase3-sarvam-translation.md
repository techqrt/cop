# Phase 3 — Sarvam Translation & English Transcript

## 1. Scope

```
Existing Original-language Transcript -> Sarvam Translation API -> English Transcript
  -> Persist English Transcript -> Authenticated + Authorized API Response
```

Phase 3 consumes the `Transcript(language=ORIGINAL)` row Phase 2 already produces and
adds exactly one new artifact: a `Transcript(language=ENGLISH)` row, exposed through
the same `GET /recordings/<id>/` endpoint Phase 2 already built. It does not call STT
again, does not touch audio, and does not implement eDAR extraction, officer review,
or export — those remain later phases (`docs/product-scope.md` §4).

## 2. Architecture

```
csc_apps.processing.stt_service._record_success (Phase 2, extended)
  on STT SUCCEEDED: get_or_create(ProcessingJob(job_type='TRANSLATION', status='PENDING'))
    + TRANSLATION_JOB_CREATED event
  (chains the same way Phase 1's upload created the STT job in the first place)

python manage.py process_pending_translation_jobs   (new, separate from
                                                       process_pending_stt_jobs)
  for each ProcessingJob(job_type='TRANSLATION', status in PENDING/RETRYING):
    csc_apps.processing.translation_service.run_translation_job(job_id)
      1. status -> RUNNING, attempt_count += 1, TRANSLATION_STARTED event
      2. original = Transcript.objects.get(recording, language='ORIGINAL')
      3. TranslationProvider.translate(original.text, original.detected_language_code, 'en-IN')
           -> SarvamTranslationProvider (csc_apps/processing/providers/sarvam/)
             -> chunk_text() splits input to Sarvam's 2,000-char limit
             -> each chunk translated sequentially, in order
             -> reassembled into one English string
      4. on success: Transcript.objects.update_or_create(language='ENGLISH', ...),
         status -> SUCCEEDED, TRANSLATION_SUCCEEDED event, ActivityLog
      5. on failure: classify via error_classification.is_retryable(), status ->
         RETRYING or FAILED, TRANSLATION_FAILED event

GET /recordings/<id>/  (extended, Phase 2's endpoint - strictly read-only, unchanged
                         route)
  -> now also returns translationStatus and transcript.english
```

## 3. Sarvam translation API

**Re-verified against https://docs.sarvam.ai in 2026-09** per the source
instructions' explicit requirement not to rely solely on the prompt - it matched
closely, with one contract detail worth calling out (§5):

- `POST https://api.sarvam.ai/translate`, header `api-subscription-key`.
- Request: `input` (text, required), `source_language_code` (required for
  `sarvam-translate:v1` - see §5), `target_language_code` (required),
  `model` (`sarvam-translate:v1`), `mode` (`formal`).
- Response: `{request_id, translated_text, source_language_code}` - no
  `target_language_code` is echoed back.
- Input limit: **2,000 characters** for `sarvam-translate:v1` (1,000 for `mayura:v1`,
  the other available model - not used here, see ADR-017).
- Errors: 400/422 (bad input), 403 (auth), 429 (rate limit), 500 (server) - the same
  flat `ApiError(status_code, body, headers)` exception shape as Phase 2's STT
  integration (both are the same underlying `sarvamai` SDK).
- Used via the official SDK's `client.text.translate(...)` convenience method
  (confirmed by reading the installed package source, `sarvamai/text/client.py`) -
  the same package Phase 2 already depends on (`requirements.txt` unchanged, no new
  dependency, per the source instructions' explicit reuse requirement).

## 4. Model selection

`sarvam-translate:v1`, not `mayura:v1` - a fixed product decision (ADR-017), not
reconsidered here. Reasons: formal-style translation fits official/professional
crash-scene reporting, and `sarvam-translate:v1` covers all 22 scheduled Indian
languages (`mayura:v1` covers 12). The model is hardcoded as a provider class
constant (`SarvamTranslationProvider.MODEL`), not environment-configurable - per the
source instructions' own guidance ("if the product decision is fixed and there is no
operational need for runtime switching, it is acceptable to define the model as a
provider constant"), matching how Phase 2 hardcodes `saaras:v3` the same way.

## 5. Source/target language handling

**Source language comes from the existing original Transcript** -
`Transcript.detected_language_code`, set by Phase 2's STT stage. Never re-detected,
never guessed.

**Contract detail found during re-verification:** `sarvam-translate:v1` does **not**
support `source_language_code="auto"` - only `mayura:v1` does. Since Phase 3 always
uses `sarvam-translate:v1` (§4), a `Transcript.detected_language_code` that is `None`
or not one of the 23 codes `sarvam-translate:v1` accepts (
`SarvamTranslationProvider.SUPPORTED_SOURCE_LANGUAGE_CODES`) cannot be translated by
this model. `SarvamTranslationProvider.translate()` checks this **before** building a
Sarvam client or making any request, raising `ProviderError('TRANSLATION_UNSUPPORTED_
INPUT', ...)` - a hard, non-retryable failure, never a silent guess (the source
instructions are explicit: "Do NOT guess the language"). This is the "explicit
mapping layer" the instructions anticipated might be needed (§11) - in practice a
validation guard rather than a code-to-code translation table, since Sarvam's own STT
and translation language vocabularies already align.

**Target language is always `en-IN`** (`csc_apps.processing.translation_service.
TARGET_LANGUAGE_CODE`), never `en-US`/`en-GB` - matches Sarvam's own language-code
convention and the rest of this codebase's existing `xx-IN` pattern (ADR-012's GPS
handling aside, every language code anywhere in this project is `xx-IN`).

## 6. Chunking strategy

`csc_apps/processing/providers/sarvam/chunker.py::chunk_text()` - a pure function, no
SDK import, independently testable. `MAX_TRANSLATION_INPUT_CHARS = 2000` is defined
in exactly this one place; nothing else in the codebase hardcodes the limit.

Algorithm: split on paragraph boundaries (runs of newlines), then sentence boundaries
(`.`/`!`/`?`/Devanagari `।`/`॥` followed by whitespace) within each paragraph,
accumulating sentences into a chunk until the next one would exceed the limit. A
single sentence longer than the limit falls back to a whitespace-boundary split
(and, only for a pathological single "word" longer than the limit, a hard character
cut - still lossless, just not at a word boundary).

**Invariant, verified by every chunking test**: `''.join(chunk_text(text)) == text`
for any input - no character lost, duplicated, or reordered - and every returned
chunk is `<= MAX_TRANSLATION_INPUT_CHARS`.

Chunks are translated **sequentially, in source order** (§Concurrency below) and
reassembled with `''.join(...)`, which reconstructs the intended text because each
chunk already carries its own trailing whitespace/punctuation from the source.

### Concurrency

Deliberately sequential, not concurrent - the source instructions frame concurrency
as something to add only "if it fits the existing architecture and provides a clear
benefit," and for typical crash-statement lengths (a handful of chunks, if that) the
benefit is marginal against the real cost: uncontrolled fan-out against Sarvam's rate
limits, and the added complexity of reassembling out-of-order concurrent responses
correctly. Order is trivially preserved as a consequence, not as something that
needed separate engineering.

## 7. Translation provider abstraction

`csc_apps/processing/providers/base.py` (Phase 0 placeholder, completed further):
- `TranslationResult` gained `source_language_code`/`target_language_code` fields
  (previously just `text`/`provider_name`/`provider_metadata`) - the canonical result
  needed to carry both, per the source instructions §10.
- `TranslationProvider.translate(text, source_language_code, target_language_code) ->
  TranslationResult` - `source_language_code` changed from `str | None` to required
  `str` (mirroring Phase 2's equivalent tightening of `SpeechToTextProvider.
  transcribe`'s signature) - the caller must resolve it before calling, never leave
  it to the provider to guess.

`csc_apps/processing/providers/sarvam/translation_provider.py` implements
`SarvamTranslationProvider(TranslationProvider)`. No Sarvam type or response shape
crosses this module's boundary. It builds its own `SarvamAI` client independently of
Phase 2's `csc_apps/processing/providers/sarvam/provider.py` (the STT provider) -
a small amount of duplicated client-construction code, chosen deliberately so the
Phase 2 file is provably untouched by this phase (verified: unchanged, confirmed via
checksum during the scope audit) rather than refactoring it to share a helper.

## 8. Processing lifecycle

Reuses `ProcessingJob` unchanged - `job_type='TRANSLATION'` was already one of Phase
0's `JOB_TYPE_CHOICES`, and `STATUS_CHOICES`/`attempt_count`/`max_attempts`/
`is_retryable`/`error_code`/`error_message` are all reused exactly as Phase 2's STT
job uses them (ADR-016's tier (a) in-job bounded retry, applied identically here -
see ADR-016 for the two-tier retry model this follows).

```
STT SUCCEEDED --(chained, stt_service._record_success)--> TRANSLATION PENDING
  --(run_translation_job)--> RUNNING --success--> SUCCEEDED
                                |
                                +--retryable, attempts remain--> RETRYING
                                +--non-retryable OR exhausted--> FAILED
```

`Recording.status` is untouched by Phase 3, same as it was by Phase 2 - it reached
`PROCESSING` after Phase 1's upload and stays there; it doesn't reach
`READY_FOR_REVIEW` until eDAR extraction exists (a later phase).

## 9. Transcript persistence

**No migration, no model change** (verified: `makemigrations --check` reports no
changes after this phase). `Transcript(recording, language)`'s existing
`unique_together` constraint, and the `language` field's existing `ORIGINAL`/
`ENGLISH` choices, already supported exactly what Phase 3 needed.

`Transcript.objects.update_or_create(recording=, language='ENGLISH', defaults={...})`
- same idempotency guarantee as the original transcript (§15 below).
`detected_language_code` on the `ENGLISH` row holds `en-IN` (the language *that row's
text* is written in - matching the `ORIGINAL` row's identical field semantics and the
API contract's `transcript.english.language` example), **not** the source language it
was translated from; the source language is kept as provenance inside
`provider_metadata` (`{"model": "sarvam-translate:v1", "chunk_count": N,
"source_language_code": "hi-IN"}`) instead - a corrected design decision (an earlier
draft of this phase stored the source language in `detected_language_code`, which
would have made the API return `"language": "hi-IN"` for the *English* transcript -
caught and fixed during testing, before merge).

## 10. Authentication & authorization

Unchanged from Phase 2 - the existing `JWTAuthentication` and the existing
owning-officer/REVIEWER/ADMIN resource-level check
(`csc_apps.recordings.views.RecordingView.get_extract`) now simply guard access to
*both* transcript versions through the one check, since both are reached via the same
endpoint and the same `Recording` row. No second authentication mechanism, no
separate authorization path for the English transcript - a user who cannot see the
original transcript cannot see the English one either, by construction (they're the
same code path).

## 11. API response

`GET /recordings/<id>/` - same route, extended shape (a documented, additive
extension - `docs/api-architecture.md` records this, not a silent break):

```json
{
  "status": true,
  "message": "Recording transcripts retrieved successfully",
  "data": {
    "recordingId": 42,
    "processingStatus": "SUCCEEDED",
    "translationStatus": "SUCCEEDED",
    "transcript": {
      "original": {"text": "gaadi tez chal rahi thi", "language": "hi-IN"},
      "english": {"text": "the vehicle was speeding", "language": "en-IN"}
    },
    "failureReason": null,
    "translationFailureReason": null
  }
}
```

- `processingStatus` keeps its exact Phase 2 meaning (the STT job's status) -
  unchanged, non-breaking.
- `translationStatus` is new: `null` until STT has succeeded and a translation job
  exists; otherwise the `TRANSLATION` job's status.
- `transcript.original`/`transcript.english` are each independently `null` until
  their respective stage succeeds - a translation is never fabricated while pending,
  and the original transcript stays visible even if translation later fails.
- `failureReason` (STT) and `translationFailureReason` (translation) each expose only
  a controlled `error_code`, never the raw provider `error_message`.
- No raw Sarvam field (`request_id`, `chunk_count`, the internal
  `source_language_code` provenance note) is ever exposed - all of it lives only in
  `provider_metadata`, which the endpoint never serializes.

This intentionally does not literally copy the source instructions' §29 example
structure (which used `snake_case` field names like `recording_id`) - Phase 2 already
established `camelCase` (`recordingId`, `processingStatus`, ...) as this project's API
convention, and the instructions explicitly say not to blindly copy the example if an
existing convention says otherwise.

## 12. Error handling

Six new error codes in `csc_apps/processing/error_classification.py` (reusing three
that already existed generically): `TRANSLATION_PROVIDER_RATE_LIMITED` (429,
retryable, new), `TRANSLATION_AUTHENTICATION_FAILED` (403 / missing key,
non-retryable, new), `TRANSLATION_MALFORMED_RESPONSE` (non-retryable, new),
`TRANSLATION_UNSUPPORTED_INPUT` (400/422, or an unsupported/missing source language,
non-retryable, reused), `TRANSLATION_PROVIDER_TIMEOUT`/`TRANSLATION_PROVIDER_
UNAVAILABLE` (network/5xx, retryable, reused from Phase 0's original placeholders),
`TRANSLATION_EMPTY_OUTPUT` (non-retryable, reused). Every code the provider can raise
is registered; an unregistered code still fails loudly (`ValueError`), unchanged
policy from Phase 0.

## 13. Retry behavior

Uses `ProcessingJob`'s existing retry fields exactly as Phase 2 does (ADR-016 tier a).
A translation retry:
- reuses the same `Recording` and the same `ProcessingJob` row (no new job created);
- reads the existing `original` `Transcript` directly - never calls STT, never
  touches `Audio`/`AudioStorage`/`SpeechToTextProvider` (verified by
  `test_retry_does_not_invoke_stt`, which asserts the STT job is untouched);
- never creates a duplicate `ENGLISH` `Transcript` (`update_or_create` +
  `unique_together`, same guarantee as §9/§15).

A chunk failing partway through translation raises immediately
(`SarvamTranslationProvider.translate()`), so `translate()` never returns a
partially-assembled result - the whole attempt is treated as failed, and no partial
English transcript is ever persisted (verified:
`test_partial_translation_failure_creates_no_english_transcript`). Per-chunk result
caching (resuming a retry from the last successful chunk rather than re-translating
everything) was **not implemented** - the source instructions explicitly permit
this ("do not implement chunk caching unless necessary"); a full retry re-translates
every chunk. Documented as a known limitation (§17), not an oversight.

## 14. Task execution

Kept as a **separate** management command,
`python manage.py process_pending_translation_jobs`, rather than folded into
`process_pending_stt_jobs` - STT and translation are independently retryable and
independently operable stages (§13); a translation-only backlog or provider
degradation shouldn't require touching the STT cron entry, and vice versa. No
Celery/RQ/Dramatiq introduced - `docs/open-decisions.md` OD-003 remains open,
unchanged, same as Phase 2.

## 15. Idempotency

Two layers, same pattern as Phase 2:
1. `run_translation_job`'s `PENDING`/`RETRYING`-only guard - a job already `RUNNING`,
   `SUCCEEDED`, or terminally `FAILED` is returned unchanged, never re-executed.
2. `Transcript`'s `unique_together(recording, language)` - `update_or_create` can only
   ever produce one `ENGLISH` row per recording, however many times a job for that
   recording is (re)run.

## 16. Security

- `SARVAM_API_KEY` is reused unchanged from Phase 2 (`Configurations.
  sarvam_api_key`) - no second credential, no new configuration surface beyond
  reusing the existing one. Never logged (translation_service's log lines carry only
  job/recording IDs, error codes, retryability, chunk counts, and durations - never
  transcript text or the key), never returned by any API response.
- Both transcript versions sit behind the one existing authorization check (§10) -
  there is no way to reach the English transcript without also being authorized for
  the original one.
- Raw Sarvam responses (per-chunk `translated_text` aside, which *is* the data, not
  metadata) never reach a response or log in bulk - `request_id` and `chunk_count`
  are the only provider-specific fields retained, and only in `provider_metadata`,
  never serialized by the API.

## 17. Testing

`csc_apps/processing/tests.py`: `TranslationChunkerTests` (16 tests),
`SarvamTranslationProviderTests` (16 tests), `TranslationServiceTests` (15 tests).
`csc_apps/recordings/tests.py`: `RecordingDetailTranslationAPITests` (8 tests), plus
5 existing `RecordingDetailAPITests` assertions updated for the new nested
`transcript.original`/`transcript.english` response shape. 55 new tests; 154 total
(99 from Phase 0-2 + 55), all passing. No real Sarvam API call is made anywhere in
the automated suite - `SarvamAI` is patched at its import site in
`csc_apps.processing.providers.sarvam.translation_provider`.

A live smoke test against the real Sarvam account (translation of both a short and a
multi-chunk synthetic English sentence into Hindi and back, end-to-end through
`SarvamTranslationProvider`) was run separately, outside the automated suite - see
the implementation report.

## 18. Known limitations

- Per-chunk retry caching is not implemented (§13) - a retry re-translates every
  chunk of a multi-chunk transcript, even ones that succeeded on a prior failed
  attempt. Acceptable for expected crash-statement lengths (a handful of chunks at
  most); would matter more for very long transcripts.
- ADR-016's tier (b) (a Recording-level retry that creates a fresh `ProcessingJob`
  after one has terminally failed) remains unimplemented for translation, same gap as
  Phase 2 has for STT - an operator would need to intervene directly (e.g. Django
  shell) to force a translation retry past `max_attempts`.
- `process_pending_translation_jobs` has no distributed lock, same as
  `process_pending_stt_jobs` - acceptable for Phase 3's single-process, cron-driven
  model (OD-003 remains open for a real task queue).
- Sentence/paragraph chunking is a deterministic regex heuristic, not a language-aware
  NLP tokenizer - it handles the Devanagari danda (`।`/`॥`) explicitly but may not
  find ideal sentence boundaries in every scheduled Indian language's punctuation
  conventions. Never loses or duplicates text regardless (the chunker's core
  invariant), just may occasionally chunk at a less-than-ideal boundary.
- Observed during live smoke testing (not something Phase 3's code causes or can
  correct): `sarvam-translate:v1` can produce anomalously long, repetitive output
  for a highly repetitive input chunk (e.g. the same sentence repeated dozens of
  times verbatim) - a model-level artifact, not a chunking or reassembly defect.
  Confirmed by re-testing with realistic, non-repetitive multi-sentence crash-style
  text: output length tracked the input proportionally (ratio ~0.63) across 5 real
  chunks, correctly reassembled in order. Real officer speech is not expected to be
  this repetitive; recorded here as an operational note, same spirit as Phase 2's
  documented SDK quirk.

## 19. Deferred to Phase 4

eDAR field extraction from the English transcript, structured 42-field JSON output,
and schema validation against `schemas/edar-schema.json` - per the source
instructions' explicit stop condition. No extraction provider was chosen or called
(`docs/open-decisions.md` OD-002 remains open for extraction).
