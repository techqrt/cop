# Phase 5 — Validation, Provenance & AI eDAR Quality Controls

## 1. Scope

Adds deterministic quality validation, field-level provenance and a structured quality
report to the AI eDAR candidate produced in Phase 4. Extends the existing
`GET /recordings/<id>/` (new public endpoints: 0). Not in scope and not done: writing
`layer=APPROVED`, officer review/edit/approval, export, new/changed AI providers,
transcript changes, resolving OD-013, Phase 6.

Principle: **AI validation is not human approval** (ADR-019).

## 2. Provenance model

Per field (`EdarFieldValue`, AI layer): `known`, `value`, `confidence`,
`source_transcript_segment` (evidence), `extraction_version` (`model/prompt/schema`).
Per record (`EdarRecord`, migration `0002`, additive/nullable): `source_transcript`
(the ENGLISH transcript), `extraction_job`, `extracted_at`, `quality_status`,
`quality_report`. An UNKNOWN field has no value, confidence or evidence.

## 3. Evidence

Evidence is a text excerpt of the English transcript (start/end times stay null - text
extraction has no audio timing). Evidence points back to the English transcript, never
the original-language transcript, and never to another recording.

## 4. Evidence matching rule

Case-folded, NFKC-normalized, whitespace-collapsed, edge-punctuation-stripped substring
match against the English transcript; an ellipsis (`...`/`…`) splits evidence into
fragments that each must match. The rule text is returned in the report
(`evidenceMatchRule`). Mismatch = per-field `evidence_not_traceable` **warning**, not an
error (the Phase 4 prompt asks for "verbatim or near-verbatim" evidence; the prompt was
deliberately not changed). A match proves the text exists in the transcript, not that it
supports the value.

## 5. Confidence semantics

Confidence is a model-generated signal, **not** a probability of truth or accuracy.
Valid: number in [0, 1] (0 and 1 included). Booleans, NaN, negatives, >1, strings are
malformed → INVALID; values are never clamped. Confidence and evidence must come
together (`provenance_incomplete`). Missing on a known field → `missing_confidence`
warning. Below 0.5 → `low_confidence` warning (a non-calibrated review-attention hint;
the value is kept). No aggregate confidence or accuracy score exists anywhere.

## 6. Validation layers

1. Provider boundary (Phase 4): structured response schema.
2. Structural/domain (`edar/schema_validation.py`, now raising `EdarValidationError`
   with a machine-readable `code`): field key, entity limits, type, resolved enum,
   confidence, provenance completeness, evidence timing.
3. Quality (`edar/quality_validation.py`): duplicates, evidence traceability, warnings,
   cross-field checks, metrics. Pure functions; never calls an AI provider (no repair
   loop).

## 7. Quality status

`VALIDATED` (no errors/warnings), `VALIDATION_WARNING` (no errors, ≥1 warning),
`INVALID` (≥1 error). None means approved or correct. INVALID candidates are never
stored as AI rows.

## 8. Deterministic checks

Errors: `invalid_field_key`, `entity_limit_exceeded` (vehicles ≤ 3), `invalid_type`
(integer, boolean, date `YYYY-MM-DD`, time `HH:MM`, multi-label lists…), `invalid_enum`
(only `injury_severity` has a resolved enum), `invalid_confidence`,
`provenance_incomplete`, `invalid_evidence_timing`, `duplicate_field`.
Warnings: `missing_evidence`, `evidence_not_traceable`, `missing_confidence`,
`low_confidence`, `duplicate_label`, `vehicle_count_mismatch`,
`vehicle_records_incomplete`, `person_count_mismatch` (only relationships the eDAR spec
itself defines; warnings because a declared count above captured records is legitimate).

**OD-013 handling:** categorical fields with `unresolved_open_decision` remain free
text; their values are not validated and no enum is invented. They are only counted
(`freeTextCategoricalFields`). OD-013 stays OPEN; what remains unresolved is listed in
`docs/open-decisions.md`.

## 9. Metrics

Counts only: `totalFields`, `knownFields`, `unknownFields`, `knownFieldsWithEvidence`,
`knownFieldsWithVerifiedEvidence`, `knownFieldsWithoutEvidence`,
`knownFieldsWithConfidence`, `lowConfidenceFields`, `freeTextCategoricalFields`,
`warningCount`, `errorCount`, plus `fieldCoverageRatio` = known ÷ total. Coverage is the
share of fields the transcript supported - not accuracy.

## 10. Persistence and failure handling

Validation runs at extraction time, before any write. Valid/warning candidate: one
`transaction.atomic()` replaces prior AI rows and sets record-level provenance/quality,
then the job is SUCCEEDED. INVALID: the job is FAILED with non-retryable
`EXTRACTION_SCHEMA_VALIDATION_FAILED`, the report is stored in
`job.provider_metadata['validation_report']`, and **a previously valid AI candidate is
untouched**. GET never validates or calls a provider.

## 11. Processing events

`quality_validation_succeeded` / `quality_validation_failed` (metadata: status, counts),
then the existing `extraction_succeeded`/`extraction_failed`. Logs and ActivityLog carry
IDs, versions and counts only - no values, evidence or transcript text.

## 12. API representation

`GET /recordings/<id>/` (same authorization as before) now returns, when extraction
succeeded: `edar.layer` (`AI`), `edar.quality {status, errors, warnings, metrics}`,
`edar.provenance {sourceTranscriptLanguage, extractionVersion, extractedAt}`, and per
field `value, known, confidence, evidence, evidenceVerified, warnings[]`. When
validation failed: `extractionStatus=FAILED`, `extractionFailureReason` (controlled
code) and `extractionIssues[{field, code, severity, message}]` (generic messages).
`evidenceVerified` is null when there is no evidence or report. camelCase, `{status,
message, data}` envelope unchanged; provider internals never exposed.

## 13. Security

No new credentials or providers. Evidence is exposed only under the existing Recording
authorization (owner, REVIEWER, ADMIN; others denied, unauthenticated 401). Issue
messages never contain values or transcript text. Transcripts are never mutated.

## 14. Testing

`edar/test_quality_validation.py` (evidence matching, confidence bounds, types, enum,
limits, cross-field, metrics, no value leakage, error codes) and
`processing/test_phase5_extraction.py` (provenance persistence, no APPROVED rows,
failed validation preserves prior candidate, reprocess replacement, API quality output,
authorization, read-only). Full suite: 271 tests passing.

## 15. Known limitations and Phase 6 handoff

- Only the current AI candidate is stored; there is no history of earlier candidates.
- Evidence match is textual; it cannot judge semantic support (paraphrased evidence
  yields a warning).
- OD-013 unresolved: categorical values are unvalidated free text.
- Failed validation is not retried automatically (non-retryable by design).
- Phase 6 (officer review/approval) must treat the quality report as reviewer guidance
  only and is the first place `layer=APPROVED` may be written.
