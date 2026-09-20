# Unknown / Missing Data Policy

## 1. Why this exists

The source brief is explicit and repeated: the AI must never fabricate a value just because
a field exists in the schema. That requires a representation for "this was asked about but
not knowable from the evidence" that is distinguishable from every other kind of absence —
mixing `null`, `""`, `false`, and the string `"unknown"` (as PMS does implicitly, having no
formal policy here) would make "the officer said they don't know" indistinguishable from
"nobody looked at this field yet" or "this field doesn't apply to this crash."

## 2. The representation

Every `EdarFieldValue` row (`docs/domain-model.md`) carries a `known` enum **in addition to**
`value`:

| `known` | Meaning | `value` |
|---|---|---|
| `KNOWN` | A value was determined (by AI extraction or officer entry) with supporting evidence. | populated |
| `UNKNOWN` | Asked about (in scope) but not determinable from the transcript, or the officer explicitly said they don't know. | `null` |
| `NOT_APPLICABLE` | The field doesn't apply to this record (e.g. a `vehicle.2.*` field when only one vehicle was involved, or `hospitalised_hospital_name` when injury severity is `no injury`). | `null` |
| `UNCERTAIN` | The AI found conflicting or ambiguous evidence and cannot commit to one value confidently enough to mark `KNOWN` — distinct from `UNKNOWN` because there *is* transcript evidence, just not resolvable evidence. | `null`, but `source_transcript_segment` is still populated so a reviewer can see what was ambiguous |

A row simply **not existing** for a given `field_key` on a given `EdarRecord` means "not yet
attempted" (e.g. extraction hasn't run yet, or a reviewer hasn't touched this field in the
`APPROVED` layer). This is different again from `UNKNOWN` — "not attempted" vs. "attempted,
came back empty" — and the UI must show these differently (a greyed-out "not yet processed"
state vs. an explicit "officer: unknown" badge).

## 3. Source-brief examples mapped to this policy

| Officer said | Field | Stored as |
|---|---|---|
| "I don't know the speed." | `speed_limit_on_road` | `known=UNKNOWN`, `value=null` |
| "The vehicle registration could not be identified." | `vehicle.<n>.vehicle_registration_number` | `known=UNKNOWN`, `value=null` |
| "I am not sure whether the driver had a licence." | `vehicle.<n>.driver_licence_status` | `known=UNCERTAIN` (there is evidence — the officer's uncertainty itself — but no resolvable value), never `driver_licence_status=false` |

## 4. Conflicting information

If the transcript contains two different values for the same field (e.g. the officer
restates the speed limit differently later in the recording), the extraction provider is
expected to prefer the later/clarifying statement (most-recent-wins) and mark `UNCERTAIN`
only when it cannot tell which statement is the correction. This heuristic is an
**ARCHITECTURAL DECISION**, not sourced from the brief (which doesn't address multi-mention
conflicts) — recorded here so it's visible, and revisitable if Phase 1 prompting shows it's
wrong.

## 5. What is finalized vs. open

**Finalized (this document):** the four-state `known` enum, its meaning, and that `value`
is always `null` unless `known=KNOWN`.

**Open** (`docs/open-decisions.md` OD-005): whether `NOT_APPLICABLE` is set automatically by
a rule engine (e.g. "if `number_of_vehicles_involved < 2`, auto-mark all `vehicle.2.*` and
`vehicle.3.*` fields `NOT_APPLICABLE`") or left for the extraction provider / reviewer to set
explicitly. Phase 0 defines the enum value; Phase 1 decides who sets it and when.
