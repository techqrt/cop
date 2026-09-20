# Data Provenance — The Three Layers of Truth

## 1. The three layers

| Layer | What | Mutability |
|---|---|---|
| **Layer 1 — Raw evidence** | `Audio` (bytes + core metadata) | Immutable once stored (ADR-002). Never overwritten by any pipeline stage. |
| **Layer 2 — AI interpretation** | `Transcript` (original + English), `EdarFieldValue` rows with `layer=AI` | Written once per successful pipeline run for that stage; a re-run (retry, or a later re-extraction) writes a **new** row/attempt rather than mutating the old one in place (see §3). Never overwritten by officer edits. |
| **Layer 3 — Human-approved record** | `EdarFieldValue` rows with `layer=APPROVED`, `EdarRecord.review_status` | Written/edited only by officer review actions. Never written by the AI pipeline directly. |

The layering is what makes ADR-008 ("AI output and officer-approved data remain separate")
concrete: they are literally different rows (`layer` column), not different versions of the
same row, so an officer edit can never destroy the AI's original output, and a re-run of the
AI pipeline can never silently overwrite an officer's approved value.

## 2. Per-field provenance contract

Every `AI`-layer `EdarFieldValue` carries (from `docs/ai-extraction-contract.md` §2/§4):

```json
{
  "field": "primary_causation_factor",
  "value": "overspeeding",
  "confidence": 0.91,
  "source": {
    "transcript_segment": "the truck was going way too fast when it hit the divider",
    "start_time": 182.4,
    "end_time": 187.8
  },
  "extraction_version": "edar-extract-v0.1.0+2026-09-15",
  "known": "KNOWN"
}
```

- `confidence` is only ever a number the extraction provider actually returned for that
  specific field, from that specific run. It is never synthesized, defaulted, or copied from
  another field ("Do not expose unsupported confidence values... do not invent confidence
  values merely for completeness" — SOURCE REQUIREMENT). A field with `known != KNOWN` has
  `confidence = null`.
- `extraction_version` identifies which extraction-provider version/prompt produced the
  value, so a later prompt change doesn't retroactively look like it produced old data.
- `APPROVED`-layer rows have their own provenance shape: `updated_by` (the reviewing
  `User`) and `updated_at`, instead of `confidence`/`source`/`extraction_version` — a human
  approval doesn't have a "confidence score" or a transcript timestamp in the same sense.

## 3. Re-runs and retries

A retried `ProcessingJob` (extraction failed, officer or system triggers a retry) produces a
**new** set of `AI`-layer rows for that `EdarRecord`, tagged with the new
`extraction_version`/attempt. Phase 0 does not keep every historical attempt indefinitely —
only the most recent `AI`-layer row per `field_key` is kept live (`unique_together:
(edar_record, field_key, layer)` in `docs/domain-model.md`), with prior attempts visible only
through `ProcessingEvent` history, not as queryable field rows. Keeping a full multi-attempt
value history is **explicitly deferred** — flagged here rather than assumed, since the source
brief doesn't specify retention requirements for superseded AI attempts
(`docs/open-decisions.md` OD-005 touches the related "who sets NOT_APPLICABLE" question; a
distinct future decision would cover attempt retention if audit requirements demand it).

## 4. GPS is out-of-band from this contract

`Recording.gps_latitude`/`gps_longitude` (ADR-012) are not `EdarFieldValue` rows and carry no
`confidence`/`source` — they're device telemetry, not an AI claim, so the provenance
question "how sure are we and where did it come from" doesn't apply the same way (the
"source" is simply "the officer's phone GPS at recording-creation time").

## 5. What Phase 0 implements vs. documents

Implemented: the `EdarFieldValue` model with the `layer`/`known`/`confidence`/`source_*`/
`extraction_version` columns (`docs/domain-model.md`), and the schema-validation check that
`confidence`+`source` are present together (`docs/ai-extraction-contract.md` §4). Documented
only: the actual UI presentation of provenance to a reviewing officer, and attempt-history
retention policy beyond "most recent wins" — both Phase 1+.
