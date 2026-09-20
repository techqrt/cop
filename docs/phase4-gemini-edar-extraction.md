# Phase 4 — Gemini AI eDAR Extraction

## 1. Scope

```
English Transcript -> Gemini structured extraction -> AI eDAR candidate
  -> Deterministic schema validation -> Persist AI extraction result
  -> Existing GET /recordings/<id>/ response
```

Phase 4 consumes the `Transcript(language=ENGLISH)` row Phase 3 already produces and
adds one new artifact type: a set of `EdarFieldValue` rows (`layer=AI`) representing
Gemini's best-effort, evidence-only reading of the transcript against the 42-field
eDAR schema. It does **not** implement officer review, officer approval, editing,
export, or any new public endpoint - those remain later phases
(`docs/product-scope.md` §4, and explicitly Phase 5/6 per the source instructions'
stop condition).

## 2. Input

The persisted `Transcript(language=ENGLISH)` text - and nothing else. Never raw
audio, never the original-language transcript, never a summary or rewritten version
of the transcript (source instructions §8, §9, §75). The full transcript text is
sent verbatim in every extraction call; Phase 4 does not chunk it the way Phase 3
chunks translation input - eDAR extraction is a single structured-output call against
the whole transcript, not a per-chunk text transform, and a spoken crash statement
(minutes, not hours) sits comfortably within Gemini's context window regardless of
the model's exact token ceiling (source instructions §74 - "if the transcript can
exceed the model context limit: STOP and document the limitation rather than
silently truncating" - this hasn't been triggered in practice for the expected input
size, and no truncation or chunking of the transcript was implemented).

## 3. Output

A set of `EdarFieldValue` rows, `layer=AI`, one per attempted eDAR field (28 flat
Module A/B/C/F/G fields, plus one per vehicle/casualty field Gemini actually
considered) - either `known=KNOWN` with a value, confidence, and supporting
transcript excerpt, or `known=UNKNOWN` with none. This is a **candidate**, not a
final record - `docs/architecture-decisions.md` ADR-018 and §7 below.

## 4. Gemini provider

**Verified against https://ai.google.dev and the installed `google-genai` 2.24.0
SDK source in 2026-09** - not assumed from memory or an old tutorial (source
instructions §10):

- Current recommended package: `google-genai` (the older `google-generativeai` is
  deprecated). Import: `from google import genai`.
- Client: `genai.Client(api_key=..., http_options=types.HttpOptions(timeout=...))`.
- One-shot text-in/structured-JSON-out call:
  `client.models.generate_content(model=, contents=, config=types.
  GenerateContentConfig(response_mime_type='application/json',
  response_json_schema=<dict>))` - confirmed by reading `GenerateContentConfig`'s
  own docstring in the SDK's `types.py`, not inferred from a web summary alone.
- `response_json_schema` accepts a raw JSON Schema dict (an alternative to the
  SDK's more restrictive `Schema`/OpenAPI-subset type, `response_schema`) supporting:
  `$id`, `$defs`, `$ref`, `$anchor`, `type`, `format`, `title`, `description`,
  `enum` (strings/numbers), `items`, `prefixItems`, `minItems`, `maxItems`,
  `minimum`, `maximum`, `anyOf`, `oneOf`, `properties`, `additionalProperties`,
  `required`, and the non-standard `propertyOrdering`. This is what
  `csc_apps.processing.providers.gemini.schema_adapter` builds.
- Errors: `google.genai.errors.APIError` (base), `ClientError`/`ServerError`
  subclasses, both carrying `.code` (HTTP status) and `.message` - read from the
  installed SDK's `errors.py`, the same flat-exception-with-status-code shape as
  Sarvam's `ApiError` (Phase 2/3), so the classification approach is consistent
  across all three providers.

## 5. Gemini model

`gemini-3.8-flash` - verified current, stable/GA ("most intelligent Flash model",
not a "-preview" label) at `ai.google.dev/gemini-api/docs/models`, and confirmed to
support structured JSON output. Chosen over `gemini-3.1-pro-preview` (higher-
capability reasoning, but preview/non-GA) specifically because a "-preview" label
signals a support/stability level inappropriate for a system feeding legal/
insurance-relevant crash reports - see ADR-018 and the qualifying-question exchange
that resolved this before implementation. The model name is a provider class
constant (`GeminiExtractionProvider.MODEL`), not environment-configurable, matching
how Phase 2/3 hardcode their own fixed model choices.

## 6. Structured-output strategy

Every extractable field is wrapped identically: `{"value": <typed>, "confidence":
<0-1 or null>, "evidence": <text or null>}`, regardless of the field's eDAR data
type - this uniform shape is what makes `known=KNOWN`/`UNKNOWN` reconciliation and
persistence field-type-agnostic. The two repeating modules become top-level arrays:
`"vehicles"` (`maxItems: 3`, Module D's field wrapper per item) and `"casualties"`
(`maxItems: 20` - an engineering safety bound, not an eDAR rule; the source document
places no cap on casualty count, unlike vehicles). `additionalProperties: false` is
set on every object in the schema as a structural backstop for "do not add fields
that are not in the schema," on top of the prompt's own instruction to the same
effect.

`temperature=0` is used - this is information extraction, not creative generation;
the most literal reading of the transcript is wanted, not varied phrasing across
runs.

## 7. eDAR schema integration

**One authoritative schema, adapted at runtime, never hand-duplicated** (source
instructions §77): `csc_apps/processing/providers/gemini/schema_adapter.py` reads
`schemas/edar-schema.json` and derives the Gemini response schema from it via a
per-`data_type` mapping (date/time/string/categorical/multi_label_categorical/
integer/integer_or_range/categorical_or_boolean/boolean/boolean_with_confidence/
ordinal/categorical_or_text/text_or_categorical/free_text -> the corresponding JSON
Schema `type`). `csc_apps/edar/tests.py::GeminiSchemaAdapterTests` is the alignment
guarantee: every field key the adapter emits is confirmed to still resolve via
`csc_apps.edar.schema_validation.resolve_field_key` - if the two schemas ever drifted
apart, this test would catch it.

`gps_coordinates` (Module A) is the one eDAR field excluded from Gemini's schema
entirely - device-captured (ADR-012), never an extraction target (source
instructions §57). Police-station jurisdiction (also Module A) *is* included in
Gemini's schema, since the eDAR source document allows it to come from spoken
information as well as GPS (ADR-014) - Gemini may report a jurisdiction only if the
officer actually names one aloud, never derived from map/GPS knowledge the model
doesn't have (source instructions §10, §58).

### Categorical values (docs/open-decisions.md OD-013)

Most categorical fields in `schemas/edar-schema.json` have
`allowed_values_status: "unresolved_open_decision"` - the eDAR source document
never enumerated their actual value lists. For these, Gemini's schema applies no
`enum` constraint (a plain nullable string) and the deterministic validator accepts
any non-empty string - the model extracts a short, natural descriptive phrase rather
than being forced into a closed list that doesn't exist yet. Only `injury_severity`
(`allowed_values_status: "resolved_from_source"`, four values straight from the eDAR
brief) gets a real `enum` constraint, both in Gemini's schema and in
`validate_extraction_entry`. This was confirmed as the right interim approach before
implementation (not assumed) - see the qualifying-question exchange that resolved
it. Fabricating enums for the other ~29 fields would have meant inventing a product
decision Phase 0 deliberately left open; OD-013 tracks resolving it properly.

## 8. Extraction prompt

`csc_apps/processing/providers/gemini/prompt.py` - a dedicated, versioned template
(`PROMPT_VERSION = "v1"`), not built inline in a view or service. Contents, per
source instructions §49: role and task, source-of-truth and no-invention rules
(explicit examples: don't infer weather/lighting/speed-limit/road-surface unless
stated; preserve uncertainty rather than converting "I believe around 30" into a
confident `30`; never confuse a stated negative with "not mentioned"; preserve
numbers/registration numbers exactly; cap vehicles at 3 and extract only people the
transcript actually supports; never substitute a system date for the crash date;
require confidence+evidence together with every non-null value), a field-by-field
reference built from the same authoritative schema (name, type, allowed values
where defined, a short extraction note per field - not just a bare field-name list),
and the structured-output instruction. The transcript is included verbatim at the
end, never summarized or rewritten first.

## 9. Provenance

Every `AI`-layer `EdarFieldValue` carries: `field_key`, `layer='AI'`, `known`,
`value`, `confidence`, `source_transcript_segment` (the evidence excerpt - no
`source_start_time`/`source_end_time`, since Gemini has no audio timestamps to
offer; `FieldSource`'s timestamp fields were made optional in Phase 4, completing
Phase 0's placeholder the same way Phase 2/3 completed the STT/translation
interfaces), and `extraction_version` - a single compact string encoding model,
prompt version, and eDAR schema version together (`"gemini-gemini-3.8-flash/
prompt-v1/schema-0.1.0"`), so a later prompt or model change never retroactively
looks like it produced an older extraction. All three components are also broken
out individually in `ProcessingEvent`/`ProcessingJob` metadata and the provider's
`provider_metadata` dict.

## 10. AI layer

`EdarRecord` is `get_or_create`d for the recording on first successful extraction
(mirroring how Phase 1's upload created the STT job) - `review_status` stays
`PENDING_REVIEW`, untouched by Phase 4. Every persisted field is `layer='AI'`.
**No code path in Phase 4 writes `layer='APPROVED'`** - verified by the scope audit
and by `ExtractionServiceTests.test_layer_is_ai_never_approved`. No confidence
threshold, however high, promotes a field to approved status; that requires an
actual officer action in a later phase (ADR-018, source instructions §60).

## 11. ProcessingJob

Reuses `ProcessingJob` unchanged - `job_type='EXTRACTION'` was already one of Phase
0's `JOB_TYPE_CHOICES`. Same lifecycle and in-job bounded retry as STT/translation
(ADR-016 tier a): `PENDING -> RUNNING -> SUCCEEDED`, or `-> RETRYING -> RUNNING`
while `attempt_count < max_attempts`, or `-> FAILED` once exhausted or non-
retryable. Translation succeeding automatically creates the `EXTRACTION`/`PENDING`
job (`csc_apps.processing.translation_service._record_success`, mirroring the
STT->TRANSLATION chain Phase 3 established) - `get_or_create` guards against ever
creating a second extraction job for one recording.

`python manage.py process_pending_extraction_jobs` - a separate command from
`process_pending_stt_jobs`/`process_pending_translation_jobs`, same reasoning as
Phase 3's STT/translation split: each stage is independently retryable and
independently operable, and a Gemini-only backlog or degradation shouldn't require
touching the other two cron entries. No Celery/RQ/Dramatiq introduced -
`docs/open-decisions.md` OD-003 remains open, unchanged.

## 12. Transcript persistence / immutability

Phase 4 never writes to `Transcript` at all - it only reads
`Transcript(language=ENGLISH)`. `ExtractionServiceTests.
test_original_and_english_transcripts_unchanged_after_extraction` verifies both
transcript rows are byte-identical before and after a run, including a run that
fails.

## 13. Validation architecture (two layers, source instructions §29)

**Layer 1 - schema/structural validation:** `csc_apps.edar.schema_validation.
validate_extraction_entry`, extended in Phase 4 (previously built but never
exercised by a concrete provider) to: (a) accept a `source` with no
`start_time`/`end_time` (Phase 4's text-only provenance), (b) type-check `value`
against the field's `data_type` (boolean, integer - explicitly rejecting `bool` as
a disguised integer, since `bool` is a Python subclass of `int` - date as ISO
`YYYY-MM-DD`, time as `HH:MM`, multi-label as a list), and (c) check a value against
a closed `enum` only where `allowed_values_status == "resolved_from_source"`
(currently `injury_severity` only). `resolve_field_key`'s existing bounds check
(unchanged since Phase 0) independently enforces the 3-vehicle cap - if a provider
implementation ever violated its own schema's `maxItems: 3` and returned a
`vehicle.4.*` field, this rejects it, defense-in-depth beyond the Gemini-schema-side
constraint.

**Layer 2 - domain reconciliation:** `csc_apps.processing.extraction_service.
_build_field_value_rows` validates every `KNOWN` field via Layer 1, then reconciles
against the full expected field-key set for this candidate (the 28 flat fields, plus
every vehicle/casualty slot Gemini's `vehicle_count`/`casualty_count` metadata says
it actually considered) so every attempted field gets a row - `KNOWN` with a value,
or `UNKNOWN` with none (`docs/unknown-data-policy.md` §2 - a missing row would
misrepresent "not yet attempted," which isn't true once Gemini has run).

**Failure handling:** any Layer 1/2 violation raises `ValueError`, caught by
`run_extraction_job` and reclassified as `ProviderError('EXTRACTION_SCHEMA_
VALIDATION_FAILED', ...)` - non-retryable (source instructions §30: "do not
endlessly retry deterministic schema violations"). The whole candidate is rejected
together; nothing is persisted for a candidate with even one invalid field
(`ExtractionServiceTests.
test_transaction_rollback_leaves_no_partial_edar_record_on_validation_failure`).

## 14. Retry behavior

Same in-job bounded retry as STT/translation (ADR-016 tier a). A retry:
- reuses the same `Recording`, the same `EnglishTranscript`, and the same
  `ProcessingJob` row (no new job created);
- never touches `Audio`, the original `Transcript`, `SpeechToTextProvider`, or
  `TranslationProvider` - verified by
  `ExtractionServiceTests.test_retry_does_not_rerun_stt_or_translation`, which
  asserts the STT/translation jobs are completely untouched by an extraction retry;
- never duplicates `EdarRecord` or produces two conflicting sets of AI-layer field
  values for one recording - a successful (re)extraction **deletes the AI layer's
  prior rows and bulk-creates the new validated set in one atomic transaction**
  (`transaction.atomic()`), so a retry after a genuinely different Gemini response
  (e.g. a later attempt that supports more fields) replaces the candidate cleanly
  rather than merging old and new rows; `unique_together(edar_record, field_key,
  layer)` (Phase 0, unchanged) still makes this structurally impossible to
  duplicate even if the delete-then-create sequence were somehow interrupted between
  two attempts.

Same known gap as Phase 2/3 (ADR-016 tier b): no mechanism exists to force a fresh
extraction attempt after a job has gone terminally `FAILED` (attempts exhausted or
non-retryable) other than direct database/admin intervention.

## 15. API behavior

`GET /recordings/<int:recording_id>/` - the **same** endpoint and route Phase 2/3
already built; **zero new public endpoints** (source instructions §40, §68,
verified via the scope audit and `csc_apps/recordings/urls.py`, unchanged since
Phase 2). Response additions, all following the existing `camelCase`/sibling-status-
field convention Phase 3 established rather than the source brief's literal
`snake_case`/nested `edar: {status, data}` example (source instructions §45 permits
this: "do not blindly copy this exact structure if existing... serializers
establish a different convention"):

```json
{
  "status": true,
  "message": "Recording transcripts and AI eDAR candidate retrieved successfully",
  "data": {
    "recordingId": 42,
    "processingStatus": "SUCCEEDED",
    "translationStatus": "SUCCEEDED",
    "extractionStatus": "SUCCEEDED",
    "transcript": {
      "original": {"text": "...", "language": "hi-IN"},
      "english": {"text": "...", "language": "en-IN"}
    },
    "edar": {
      "layer": "AI",
      "fields": {
        "crash_type": {"value": "rear-end collision", "known": "KNOWN", "confidence": 0.9},
        "weather_at_time_of_crash": {"value": null, "known": "UNKNOWN", "confidence": null},
        "vehicle.1.vehicle_type": {"value": "motorcycle", "known": "KNOWN", "confidence": 0.85}
      }
    },
    "failureReason": null,
    "translationFailureReason": null,
    "extractionFailureReason": null
  }
}
```

`extractionStatus` is `null` until translation has succeeded (nothing to be pending
on before that - same reasoning `translationStatus` already established).
`edar` is `null` until `extractionStatus == "SUCCEEDED"` - an AI candidate is never
fabricated while processing is incomplete. `edar.fields` includes **every** field
Gemini attempted, `KNOWN` or `UNKNOWN` alike - not just the populated ones - so a
future review UI can render a complete form. Deliberately minimal for Phase 4:
`source_transcript_segment`/`extraction_version` are persisted but not exposed
through this endpoint - per-field evidence/provenance display is explicitly Phase
5's job (source instructions' stop condition), not built ahead of that phase.
`extractionFailureReason` exposes only the controlled `error_code`, never the raw
Gemini error message.

The endpoint remains **strictly read-only**: it never calls Gemini, never starts or
retries any job, never creates an `EdarRecord` - verified by
`RecordingDetailExtractionAPITests.test_get_never_creates_or_mutates_an_extraction_job`.

## 16. Authentication

Unchanged - the existing `JWTAuthentication`. No second mechanism introduced.

## 17. Authorization

Unchanged - the existing owning-officer/`REVIEWER`/`ADMIN` resource-level check on
`GET /recordings/<id>/` now simply also guards the `edar` data, through the same one
check every other field on this response already goes through. A user who cannot
see the transcripts cannot see the AI eDAR candidate either, by construction (same
code path, same `Recording` row) - never authorized by `EdarRecord` ID,
`ProcessingJob` ID, or any other identifier alone.

## 18. Security

- `GEMINI_API_KEY` lives in `.env` only (git-ignored), read via `Configurations.
  gemini_api_key`, never logged (`GeminiExtractionProvider` logs nothing containing
  it; `extraction_service`'s log lines carry only job/recording IDs, error codes,
  retryability, field counts, and durations), never returned by any API response.
- The full transcript is sent to Gemini (required for extraction to work at all -
  §2) but never logged in full anywhere in this codebase; the full raw Gemini JSON
  response is parsed and reconciled, never logged or persisted verbatim (§19 below).
- Gemini's output is treated as untrusted provider input end-to-end: parsed, then
  validated against the authoritative schema before a single row is persisted -
  never trusted merely because it matched the requested JSON shape.
- Unauthorized users cannot reach AI eDAR data - §17.
- Error responses expose only a controlled `error_code`, never a Gemini stack trace,
  internal path, or the API key.

## 19. Raw Gemini output

**Never persisted or logged in full** (source instructions §28). Only the
reconciled, validated `{field_key, value, confidence, source_transcript_segment}`
per field is stored (via `EdarFieldValue`), plus the small, explicitly-chosen
`provider_metadata` dict on `ProcessingJob`/`ProcessingEvent` (`model`,
`prompt_version`, `schema_version`, `vehicle_count`, `casualty_count`) - never the
complete raw JSON response, which could otherwise carry restated personal
information from the transcript inside every field's `evidence` string with no
independent purpose beyond what's already captured per-field.

## 20. Testing

`csc_apps/edar/tests.py`: `ValidateExtractionEntryTests` extended (12 new cases:
missing timestamps, date/time/integer/multi-label type checks, resolved-enum
accept/reject, unresolved-categorical free-text acceptance, vehicle bounds) and a
new `GeminiSchemaAdapterTests` (11 cases - schema/alignment guarantees).
`csc_apps/processing/tests.py`: `GeminiExtractionProviderTests` (16 cases - success,
omission-not-fabrication, empty input, missing key, malformed/non-object JSON,
empty response, 400/403/429/500 API errors, timeout, unhandled network error) and
`ExtractionServiceTests` (18 cases - transcript retrieval, schema supplied,
persistence, `UNKNOWN` backfill, full 28-field coverage, `layer=AI` only,
transcript immutability, events/activity log, retryable/non-retryable
classification, no-STT/no-translation-rerun on retry, no duplication, atomic
rollback on validation failure, idempotent re-invocation, missing-transcript
failure, vehicle-cap defense-in-depth).
`csc_apps/recordings/tests.py`: `RecordingDetailExtractionAPITests` (14 cases - all
authorization scenarios, pending/running/failed/succeeded response shapes, `UNKNOWN`
fields represented not omitted, existing transcript response unaffected, no raw
Gemini/evidence exposure, envelope conformance, read-only guarantee).

**77 new tests; 223 total (146 from Phase 0-3 + 77), all passing.** No real Gemini
API call is made anywhere in the automated suite - `genai.Client` is patched at its
import site in `csc_apps.processing.providers.gemini.provider`.

A live smoke test against a real Gemini account was not run for this phase - no
`GEMINI_API_KEY` was available in this environment at implementation time (unlike
Phase 2/3, where a Sarvam key was shared mid-session). Available on request the same
way Phase 2/3's live checks were performed once a key is provided.

## 21. Known limitations

- No live Gemini smoke test was performed (§20) - the mocked test suite is thorough,
  but a live end-to-end run (real transcript in, real structured JSON out, through
  the real `response_json_schema` constraint) has not yet been verified the way
  Phase 2/3's Sarvam integrations were.
- ADR-016 tier (b) applies to extraction too, same as STT/translation: no mechanism
  exists to force a fresh attempt after a job has gone terminally `FAILED` beyond
  direct database/admin intervention.
- `process_pending_extraction_jobs` has no distributed lock, same as the other two
  commands - acceptable for Phase 4's single-process, cron-driven model (OD-003
  remains open).
- OD-013 (categorical enumerations) is a real, acknowledged gap: most categorical
  fields are extracted as free descriptive text rather than a true closed enum,
  which is workable but means two similar transcripts could produce differently-
  worded values for what should be the same category.
- `MAX_CASUALTIES = 20` is an engineering safety bound chosen without product input
  (the eDAR source places no cap on casualty count) - unlikely to matter in
  practice, but a real mass-casualty scene with more than 20 named individuals
  would have the excess silently uncounted by Gemini's own array cap (not
  represented as `UNKNOWN` rows, since the service never asked Gemini to consider
  a 21st casualty slot).
- No per-field NLP/semantic validation beyond type/enum/date/time checks - e.g. an
  extracted `vehicle_registration_number` is only checked for being a string, not
  matched against a real registration-plate regex (the eDAR schema itself notes
  this format is not yet defined - `docs/domain-model.md`).

## 22. Deferred to Phase 5 / Phase 6

Phase 5: field-level provenance/evidence display through the API, confidence/
evidence refinement, validation/quality controls, AI-extraction auditability tooling
- the underlying data (`source_transcript_segment`, `extraction_version`,
per-field `confidence`) is already persisted by Phase 4, just not yet exposed or
refined further. Phase 6: officer review, officer edits, officer approval
(`layer='APPROVED'` is a real, unused enum value on `EdarFieldValue.layer` today,
waiting for that phase), and eDAR export. No part of either phase was implemented
now - verified by the scope audit (§ in the final report).
