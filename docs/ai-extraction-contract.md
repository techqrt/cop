# AI Extraction Contract

## 1. Inputs (fixed, per SOURCE REQUIREMENT)

1. The **English transcript** (never the original-language transcript directly — ADR-004).
2. The **fixed eDAR schema** (`schemas/edar-schema.json`), passed in full so the model knows
   the complete, closed set of fields it may populate.
3. Extraction instructions/rules (a prompt/config, provider-specific, living in
   `csc_apps/processing/providers/<provider>/`, not implemented in Phase 0).

## 2. Output shape (provider-agnostic, canonical)

The `ExtractionProvider.extract(...)` interface returns a list of field results, one entry
per field the model found evidence for (fields with no evidence are simply absent from the
list, not present-with-a-guessed-value — see `docs/unknown-data-policy.md`):

```json
{
  "field": "primary_causation_factor",
  "value": "overspeeding",
  "confidence": 0.91,
  "source": {
    "transcript_segment": "the truck was going way too fast when it hit the divider",
    "start_time": 182.4,
    "end_time": 187.8
  }
}
```

This is the exact shape given in the source brief. It is **never** rendered by the provider
as a Markdown table or any other display format — that would make "canonical output" and
"display output" the same artifact, which ADR-007 explicitly rejects. A UI renders this
structure as a table; the structure itself is the contract.

## 3. Hard constraints on the provider

- **Closed vocabulary of fields.** The provider may only emit `field` values that exist in
  `schemas/edar-schema.json` (base field key for module A/B/C/F/G, or a valid
  `vehicle.<n>.<field>` / `casualty.<n>.<field>` for D/E, `n` within `max_repetitions`).
  Anything else is a schema-validation failure (`docs/processing-pipeline.md` stage
  "Schema Validation"), not silently dropped or silently accepted.
- **No fabrication.** If the officer's speech doesn't support a value, the field is omitted
  from the result entirely — not defaulted, not guessed at a "safe" value, not given a low
  confidence score to signal doubt (`docs/unknown-data-policy.md` is the mechanism for
  marking "asked about but not knowable", and it is applied by the *persistence* layer, not
  invented by the provider). Source brief, verbatim: *"If the officer says 'I don't know the
  speed,' do NOT output `speed_limit = 60`."*
- **No confidence without evidence.** `confidence` and `source` are required together — a
  field result with a `value` and no `source` span is treated as a malformed response
  (Schema Validation failure), because an un-sourced confidence number is exactly the kind
  of fabrication this contract exists to prevent.
- **`vehicle`/`casualty` indices are stable within one extraction run**, but not guaranteed
  stable across a retried extraction — the persistence layer re-derives indices by writing
  a fresh `AI`-layer `EdarFieldValue` set per successful extraction attempt rather than
  diffing against the previous attempt (simpler, avoids silently merging two different
  runs' guesses about "which vehicle is vehicle 2").

## 4. Schema validation (persistence-side, not provider-side)

Before any `EdarFieldValue` row is written, every result entry is checked against
`schemas/edar-schema.json`:

1. `field` resolves to a real schema field (module + optional index within bounds).
2. `data_type` compatibility — e.g. `boolean` fields get `true`/`false`, not `"yes"`.
3. `confidence` is a float in `[0, 1]` when present.
4. `source.start_time <= source.end_time`, both within the transcript's duration.

A failing entry fails that field only (logged as a `ProcessingEvent`, `docs/
observability.md`) — it does not fail the whole extraction job unless every entry fails or
the response is structurally unparseable, per `docs/error-retry-strategy.md`'s
non-retryable classification for malformed AI responses.

## 5. What Phase 0 implements vs. documents

Implemented: the `ExtractionProvider` interface and the canonical result shape (as a
`dataclasses.dataclass`, `csc_apps/processing/providers/base.py`), plus the schema-validation
function operating on that shape (`csc_apps/edar/schema_validation.py`) with unit tests.
Documented only: the actual prompting/provider implementation, which needs a chosen LLM
provider (`docs/open-decisions.md` OD-002) and is Phase 1.
