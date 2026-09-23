# Phase 8 — Export

## 1. Purpose

A single-recording JSON export of the officer-approved eDAR dataset, downstream of
Phase 6 approval. Not in scope and not done: CSV/PDF/Excel/XML/Word export, bulk or
scheduled export, email delivery, transcript or raw-audio export, analytics,
reporting, dashboards, Phase 9/10.

## 2. Endpoint

**`GET /recordings/<id>/export/`** (the only new endpoint). No new URL style - a
resource-oriented route nested under the same `Recording` resource `GET /<id>/`
and `POST /<id>/edar/approve/` already use.

## 3. Authentication

The existing Bearer/JWT mechanism, unchanged. Unauthenticated: 401.

## 4. Authorization

Identical to `GET /recordings/<id>/`: the recording's owning officer, or any
REVIEWER/ADMIN. Reuses the exact same `RecordingView._get_authorized_recording`
helper GET and approve already call - not a second, independently-maintained
check. Authorization is applied before the eDAR data is ever read, not filtered
out afterward. An unauthorized officer gets the same 400 "Not allowed to access
this recording" GET already returns for another officer's recording - not a
distinct "forbidden" response.

## 5. Approval requirement

Export is rejected (400, via `csc_apps.edar.export_service.ExportNotAvailable`, a
`ValueError` subclass mapped through the existing `Common().exception_handler`
the same way every other business-rule rejection in this codebase is) unless:
- an `EdarRecord` exists for the recording (extraction succeeded at least once), and
- `EdarRecord.review_status == 'APPROVED'` exactly, and
- at least one `layer='APPROVED'` row actually exists (a defensive check against a
  state Phase 6's own invariant should already prevent).

No eDAR extraction, a failed extraction, and an AI-only (not yet approved) record
all produce the same rejection - no new state was invented to distinguish them
further.

## 6. Export schema

Per the qualifying-questions decision, the eDAR dataset is **nested by module**,
not the flat dotted-key map `GET /recordings/<id>/`'s `edar.fields` uses:

```json
{
  "recordingId": 1,
  "caseFirNumber": "FIR-2026-042",
  "reviewStatus": "APPROVED",
  "approvedBy": {"userId": 1, "name": "...", "email": "..."},
  "approvedAt": "2026-...",
  "gpsCoordinates": {"latitude": 22.30, "longitude": 73.18, "capturedAt": "..."},
  "eDAR": {
    "crashIdentification": {"crash_date": {"known": "KNOWN", "value": "2026-03-12"}, "crash_time": {...}, "road_name": {...}, "police_station_jurisdiction": {...}, "case_fir_number": {...}},
    "roadEnvironment": {"road_type": {...}, "...": {}},
    "crashCircumstances": {"crash_type": {...}, "...": {}},
    "vehicles": [{"vehicle_type": {...}, "...": {}}, {"...": {}}],
    "casualties": [{"person_type": {...}, "...": {}}],
    "infrastructureObservations": {"street_lighting_functional": {...}, "...": {}},
    "officerAssessment": {"suspected_alcohol_drug_involvement": {...}, "witness_present": {...}, "officer_remarks": {...}}
  }
}
```

Module names are the source instructions' own headings (§11 A-G). `vehicles`/
`casualties` are arrays (one object per approved vehicle/casualty, in index order)
- never flattened dotted keys, never re-numbered, never padded with fabricated
entries. Built by `csc_apps.edar.export_service.build_export`, which iterates the
canonical `schemas/edar-schema.json` (via the existing `load_schema()`) - no
second, independently-maintained field list.

`gps_coordinates` is the one Module A field excluded from the eDAR object
entirely - see §8.

## 7. Known/Unknown semantics

Every field is `{"known": ..., "value": ...}`, exactly the shape
`GET /recordings/<id>/`'s `edar.fields`/`edar.approved.fields` already use.
`known` is one of `KNOWN`/`UNKNOWN`/`NOT_APPLICABLE`/`UNCERTAIN` - the existing
`EdarFieldValue.KNOWN_CHOICES`, no new convention. A field with no approved row at
all (should not happen under Phase 6's own invariant, but defensively handled)
renders as `{"known": "UNKNOWN", "value": null}` rather than being omitted from
the export's structure - the export always represents the complete logical
42-field/7-module shape, filling any gap explicitly rather than silently dropping
a field a consumer would expect to find.

## 8. AI vs APPROVED behavior

**Only `layer='APPROVED'` is ever read.** `csc_apps.edar.export_service` contains
no query against `layer='AI'` at all - not as a fallback, not for comparison. If
the approved snapshot is missing or incomplete, export fails; it is never
silently filled from the AI candidate. Verified directly: a test approves a
recording, then mutates the AI row's value afterward (simulating a hypothetical
AI-row change) and confirms the tampered value never appears in the export and
the original approved value is unaffected.

`gpsCoordinates` is read from `Recording.gps_latitude`/`gps_longitude`/
`gps_captured_at` directly, not from the eDAR layer system at all - it is
device-captured (ADR-012) and was deliberately excluded from Gemini extraction
from Phase 4 onward, so there has never been an AI or APPROVED `EdarFieldValue`
row for `gps_coordinates` to read in the first place. Reading it from `Recording`
is not an AI-to-APPROVED leak.

No AI provenance (confidence, evidence, extraction_version) appears anywhere in
the export - the export represents the officer-approved result, not the AI's
reasoning, consistent with Phase 5/6's own provenance boundary.

## 9. Error behavior

| Condition | Status | Message source |
|---|---|---|
| Unauthenticated | 401 | existing `SerializerValidations` |
| Recording not found | 400 | existing `_get_authorized_recording` |
| Recording belongs to another officer (not REVIEWER/ADMIN) | 400 | existing `_get_authorized_recording` |
| No eDAR extraction exists | 400 | `ExportNotAvailable` |
| eDAR exists but not approved | 400 | `ExportNotAvailable` |
| Approved but structurally empty (defensive) | 400 | `ExportNotAvailable` |
| Success | 200 | — |

All reuse the existing `{status, message, error}` shape via
`Common().exception_handler`'s `ValueError` branch - no new error style.

## 10. Security

- Fixed export contract: no client-supplied field name, model name, column name,
  or format selection anywhere in the request (the request body is empty; only
  `recording_id` comes from the URL, same as GET/approve).
- IDOR: covered by the same authorization tests as GET/approve, applied at the
  query level before any data is read.
- No transcript text, raw audio, provider metadata, prompts, or Gemini/Sarvam
  internals anywhere in the export or its errors (dedicated test).
- No secrets/JWT in the response.

## 11. Query/performance behavior

Per export: one recording lookup (authorization), one `EdarRecord` lookup, one
`EdarFieldValue` query for all `layer='APPROVED'` rows (a single `filter()`, not
per-field or per-vehicle) - a small, fixed number of queries regardless of how
many vehicles/casualties the record has. Verified with a query-count test
(bounded under 10 total queries for one export).

## 12. Export actions are not logged in Phase 8

Considered and deliberately deferred, not overlooked. `ActivityLog.ACTION_CHOICES`
is currently `('Create', 'Update', 'Delete')` - none fit "read/export"
semantically, and adding a new choice to that field is a model change Django's
migration framework tracks (an `AlterField` operation would be generated),
conflicting with this phase's explicit preference for zero migrations. Per source
instructions §19 ("if this would require creating a new audit architecture, defer
it to Phase 9. Do not overbuild."), export access logging is deferred to Phase 9.

## 13. Known limitations

- Export access is not audited (see §12).
- JSON only - no CSV/PDF/Excel, per this phase's fixed scope.
- No bulk/multi-recording export.
- A normal enveloped API response, not a downloadable file attachment (no
  `Content-Disposition`) - PMS has no file-download precedent anywhere to diverge
  from, and introducing one here would be a new response architecture the source
  instructions explicitly ask to avoid absent a real requirement for it.

## 14. Explicit Phase 9/10 deferrals

- Export audit logging (Phase 9 - broader audit/observability hardening).
- Any security-hardening program, alerting, or dashboards (Phase 9).
- Deployment/infrastructure changes (Phase 10).
