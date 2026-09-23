# Phase 6 — Officer Review + Approval

## 1. Scope

Adds the human review/approval workflow on top of the existing AI eDAR candidate:
field-level editing plus whole-record approval, creating a separate, immutable
`layer=APPROVED` snapshot. Not in scope and not done: search/history, export,
PDF/CSV, reporting/analytics, any change to Sarvam/Gemini behavior, automatic
approval, Phase 7+.

Principle (non-negotiable): **Gemini AI output is immutable.** Approval only ever
creates `EdarFieldValue(layer='APPROVED', ...)` rows; it never updates, deletes, or
re-tags an AI row.

## 2. Data model

No new models, no migration. Reused as-is:
- `EdarRecord.review_status` (`PENDING_REVIEW`/`IN_REVIEW`/`APPROVED`),
  `.reviewed_by`, `.reviewed_at` - already existed, unused until now.
- `EdarFieldValue.layer` (`AI`/`APPROVED`), `.updated_by`, `.updated_at` - the
  APPROVED layer was already designed in, just never written to.
- `unique_together (edar_record, field_key, layer)` - the same row shape holds
  both layers.

## 3. Workflow

```
AI eDAR (existing) -> officer reviews via GET -> officer submits edits + approves
  -> server validates -> APPROVED snapshot created atomically -> audit trail
```

Review itself has no separate endpoint - the existing `GET /recordings/<id>/`
already exposes everything an officer needs to review (value, known, confidence,
evidence, evidenceVerified, warnings, quality status, extraction version, source
transcript language). Adding a second read endpoint would have duplicated it.

## 4. API

**`POST /recordings/<id>/edar/approve/`** (new; the only new endpoint).

- **Auth:** Bearer token, same as every other endpoint.
- **Authorization:** identical rule to `GET /recordings/<id>/` - the recording's
  owning officer, or any REVIEWER/ADMIN. Resolves OD-001's "self-review vs.
  separate reviewer" question as "both may act" rather than picking one
  exclusively - the smallest change from GET's already-established rule.
- **Request body:** `{"fields": {field_key: {"known": ..., "value": ...}}}` - a
  *partial* edit set. A field_key absent from it is copied through from the AI
  candidate unchanged. Empty/omitted `fields` approves the AI candidate as-is. No
  AI provenance (confidence/evidence/extraction_version) is ever accepted from the
  client - the server owns that, and the client never submits who is approving or
  when.
- **Response:** the exact same shape `GET /recordings/<id>/` returns - no second
  response format. Immediately reflects `edar.reviewStatus="APPROVED"` and
  `edar.approved`.

`GET /recordings/<id>/` is unchanged in every existing field; two fields were
added to `edar`: `reviewStatus` (always present once an EdarRecord exists) and
`approved` (null until approved, then `{reviewedBy, reviewedAt, fields}`). The
existing `edar.fields` keeps meaning exactly what it always meant - the AI
candidate, never overwritten.

## 5. Edit scope

An edit may only target a field_key the AI candidate already has a row for
(KNOWN or UNKNOWN) - confirming/correcting what Gemini produced, not adding a
vehicle/casualty entry Gemini never reported. A field_key outside that set is
rejected (`ValueError`, mapped to the standard 400 envelope). This was a
deliberate scope decision, not a technical limit - see §12 Known limitations.

## 6. Validation

Officer-submitted values are validated with the same structural/type engine
Phase 4/5 apply to AI output (`csc_apps.edar.schema_validation`), extended with
one new entry point, `validate_approved_value(field_key, known, value)`, that
reuses the existing `_validate_value_type` rather than a second validation
engine. One real gap this surfaced: `_validate_value_type` never checked plain
string-typed fields (`string`, `categorical`, `ordinal`, `categorical_or_text`,
`text_or_categorical`, `free_text`) at all - AI output never needed that check
because Gemini's own request-time JSON schema already constrained the type
before Python ever saw it, but officer input has no such upstream gate. Fixed as
part of Phase 6 (a Phase 6 acceptance test caught it), benefiting AI validation
too as defense-in-depth.

`known` must be one of the existing `EdarFieldValue.KNOWN_CHOICES` states, no new
convention. `KNOWN` requires a non-null value passing type validation; any other
state requires a null value - the same shape every AI UNKNOWN row already has.

## 7. Approved snapshot

Always the complete logical eDAR dataset the AI candidate represents: every
field_key on the AI candidate gets exactly one APPROVED row, either copied
verbatim or overridden by the officer's edit. Never a partial/diff snapshot on
disk - the diff only exists in the request body.

## 8. AI immutability

Enforced structurally, not by convention alone: `approve_edar` never issues an
`UPDATE`/`DELETE` against a `layer='AI'` row - it only ever reads them (to build
the APPROVED snapshot and the audit's before/after) and writes new
`layer='APPROVED'` rows. Verified by a dedicated test class comparing every AI
column (value, confidence, evidence, extraction_version, layer) byte-for-byte
before and after an approval with edits.

## 9. Audit trail

Uses the existing `ActivityLog` (`action='Update'`, `model='EdarRecord'`) - no
second audit system. `details` carries `recording_id`, `edar_record_id`,
`approved_field_count`, and `changed_fields`: a list of
`{field, aiKnown, aiValue, approvedKnown, approvedValue}` for every field that
actually changed (not the whole snapshot) via the existing JSON `details`
mechanism. Officer identity and timestamp come from `ActivityLog.user` and
`.created_on` (and redundantly from `EdarRecord.reviewed_by`/`.reviewed_at`) -
never from the request body.

## 10. Recording lifecycle

Two changes to the existing (already-defined, previously-unused)
`PROCESSING -> READY_FOR_REVIEW -> IN_REVIEW -> COMPLETED` path
(`docs/recording-state-machine.md`), deferred through Phases 2-5 pending an
actual review mechanism:
1. `csc_apps.processing.extraction_service` now transitions a Recording to
   `READY_FOR_REVIEW` on successful extraction (guarded by `can_transition`, a
   no-op if the recording is already past that point).
2. `approve_edar` walks the recording forward to `COMPLETED` through whatever
   legal hops remain (`READY_FOR_REVIEW -> IN_REVIEW -> COMPLETED`), so a
   candidate extracted before change (1) existed still approves correctly. No
   new transition edge was added to `ALLOWED_TRANSITIONS`.

## 11. Concurrency / atomicity

One `transaction.atomic()` block: re-validate not-already-approved, delete any
stray APPROVED rows (defensive idempotency, mirroring the AI-layer replace
pattern), bulk-create the new APPROVED rows, update `review_status`/`reviewed_by`/
`reviewed_at`, advance the Recording state machine, write the ActivityLog entry.
Any failure anywhere in that block rolls all of it back - verified with a test
that forces `ActivityLog.record` to raise mid-transaction and confirms zero
APPROVED rows, unchanged `review_status`, and unchanged AI rows afterward.

## 12. Known limitations / open decisions

- Only one APPROVED snapshot is supported; a second approval attempt is
  rejected outright, not versioned (matches "prefer rejection... if the current
  model represents one current approved snapshot").
- An officer cannot add a vehicle/casualty record the AI candidate never
  reported - only correct/confirm what's already there (§5). Revisit if this
  turns out to be a common real-world need.
- `VALIDATION_WARNING` never blocks approval - the officer's review is the
  mechanism for resolving warnings, per source instructions §45. No
  "acknowledge warnings" flag was added; the warnings are visible in the same
  GET response the officer already reviews from.
- OD-001 ("does a Reviewer role exist, how is review assigned") is narrowed but
  not fully closed: this phase answers "who may approve" (owner + REVIEWER/ADMIN)
  without adding an assignment/queue mechanism.
