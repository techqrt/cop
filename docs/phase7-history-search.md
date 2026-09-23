# Phase 7 — History + Search

## 1. Purpose

A read/query layer over the existing domain model (`Recording`, `ProcessingJob`,
`EdarRecord`) so an authenticated user can browse and filter their own recording
history. No new history table, no new domain entity - `Recording` is, and remains,
the primary history entity. Not in scope and not done: export, PDF/CSV, reporting,
analytics, dashboards, notifications, bulk operations, any change to Sarvam/Gemini,
any change to Phase 6 approval behavior, Phase 8+.

## 2. API

**`GET /recordings/`** (shares its path with the existing `POST /recordings/`
upload endpoint, per source instructions §5/§29/§30 - not a second `/history/`
route). Both are still fully independent, separately-decorated view functions
(`RecordingViewController.upload`/`.list_recordings`), each with its own request/
response serializer and its own method restriction; a small combined
`recordings_root` view exists purely because Django's `path()` matches by URL
pattern only, not HTTP method, so one path can register only one view, and
drf-spectacular needs a single decorated function to introspect both operations
(via its own documented `@extend_schema(methods=[...])` mechanism).

- **Auth:** Bearer token, required.
- **Authorization:** every role - officer, REVIEWER, ADMIN - sees only recordings
  they personally created. See §4.
- **Response:** the existing `{status, message, data}` envelope; `data` is the
  existing PMS pagination shape (`csc_apps.common.utils.Utils.add_page_parameter`):
  `{data: [...], presentPage, totalPage, nextPageUrl?, previousPageUrl?}`.

`GET /recordings/<id>/` and `POST /recordings/<id>/edar/approve/` are unchanged -
full detail remains their job; this endpoint never duplicates transcript text, eDAR
fields, quality reports, or audit details.

## 3. Supported search/filter fields

| Filter | Source | Type | Example | Implemented |
|---|---|---|---|---|
| `status` | `Recording.status` | exact, whitelisted enum | `?status=COMPLETED` | Yes |
| `review_status` | `EdarRecord.review_status` (reverse 1:1) | exact, whitelisted enum | `?review_status=APPROVED` | Yes |
| `road_name` | `Recording.road_name` (Phase 1 officer-confirmed column) | case-insensitive partial (`icontains`) | `?road_name=NH 48` | Yes |
| `case_fir_number` | `Recording.case_fir_number` (Phase 1 officer-confirmed column) | exact match | `?case_fir_number=FIR-2026-001` | Yes |
| `created_from` / `created_to` | `Recording.created_at` (date part) | ISO date range | `?created_from=2026-01-01&created_to=2026-03-31` | Yes |
| `page_num` / `limit` | pagination | integer | `?page_num=2&limit=20` | Yes (limit capped at 100) |
| crash date | `EdarFieldValue` (AI/APPROVED, layered) | - | - | **No** - see §6 |
| `police_station_jurisdiction` | `Recording.police_station_jurisdiction` | - | - | **No** - not requested; can be added the same way as `road_name` if needed |
| sort field selection | - | - | - | **No** - deterministic default (`-created_at, -recording_id`) only, per source instructions §22 ("if not explicitly required, do not add") |
| transcript text | `Transcript.text` | - | - | **No** - explicitly out of scope, see §7 |
| generic `field_key=value` | `EdarFieldValue` | - | - | **No** - explicitly out of scope, see §8 |

All filters combine with AND semantics (`?status=COMPLETED&road_name=NH 48` means
both, never either). Query params are validated by `ListRecordingsRequestSerializer`
before ever reaching the queryset - `status`/`review_status` are DRF `ChoiceField`s
against the existing enums (no arbitrary value reaches the ORM), `road_name`/
`case_fir_number` are length-capped `CharField`s, dates are `DateField`s. Invalid
values (bad enum, bad date, non-numeric page) are rejected with the project's
standard 400 validation response, never silently ignored.

## 4. Authorization

| Actor | Accessible in the list |
|---|---|
| Owner officer | Their own recordings |
| Other officer | Empty - never another officer's recordings |
| REVIEWER | Their own recordings only |
| ADMIN | Their own recordings only |

This is a deliberate Phase 7 scope decision (confirmed in the qualifying-questions
round), and it is **narrower** than the existing `GET /recordings/<id>/`
authorization (owner OR any REVIEWER/ADMIN). A REVIEWER/ADMIN can still open any
recording directly via `GET /recordings/<id>/` if they already have the ID -
unchanged Phase 2-6 behavior - but will not see other officers' recordings in
their own history list. Filtering does not change this: every filter is applied on
top of `Recording.objects.filter(officer=requesting_user)`, at the database query
level, before pagination - a filter can narrow the requester's own accessible set,
never widen it. Verified directly with IDOR-style tests (search by another
officer's exact case/FIR number, or by their road name, returns nothing).

## 5. Query behavior / performance

Per page (regardless of page size, up to the 100-row cap): one `COUNT` query
(pagination), one `SELECT` for the page's `Recording` rows, one batched
`ProcessingJob` query (`recording_id__in=<page ids>`, grouped in Python by
`(recording_id, job_type)`, keeping only the most recent per stage), and one
batched `EdarRecord` query for review status - four to five queries total, never
one query per recording. Verified with two tests: a request against 2 recordings
and the same request against 10 recordings produce an identical query count, and a
10-recording page stays under 10 total queries.

## 6. Date filters

Only `created_from`/`created_to`, filtering `Recording.created_at` (recording
creation date - unambiguous, already present on every recording regardless of
processing/review state). Crash-date filtering (the eDAR-extracted `crash_date`,
which lives only on `EdarFieldValue` and would need an AI-vs-APPROVED layer
preference plus a join per candidate row) is **not implemented** - a deliberate,
confirmed scope decision, not an oversight.

## 7. Transcript search

**Full transcript text search is not part of Phase 7**, per source instructions
§24 - a separate capability with real performance/privacy implications of its own.

## 8. AI/APPROVED semantics in search

`road_name`/`case_fir_number`/`police_station_jurisdiction` are `Recording`'s own
officer-confirmed columns from Phase 1 - not an eDAR AI or APPROVED value, so there
is no AI-vs-APPROVED ambiguity to resolve for them at all (Phase 4's extraction
pipeline separately cross-checks these against the transcript into `EdarFieldValue`,
but the list never reads that layered value). No generic `field_key=value` search
over `EdarFieldValue` was built (source instructions §25 - avoid turning this into
a query builder); the one eDAR-derived field the list *does* expose is
`reviewStatus` (`EdarRecord.review_status` - a single, unambiguous per-record value,
not a per-field AI/APPROVED pair), so `APPROVED` in a list row means exactly what it
means on the approval endpoint: an immutable human-approved snapshot exists. The AI
candidate's field-level values are never surfaced in the list at all.

## 9. Pagination

Reuses the existing, already-ported PMS convention exactly -
`django.core.paginator.Paginator` + `csc_apps.common.utils.Utils.add_page_parameter`,
`Configurations.pagination_count` (10) as the default `limit`. `page_num` beyond
the last page raises the same `'Page limit exceed!'` `ValueError` PMS's own list
views already raise (mapped to the standard 400 response). `limit` is capped at
100 to prevent unbounded retrieval (source instructions §8) - the smallest
practical safeguard, not a new protocol.

## 10. Sorting

Deterministic default only: `-created_at, -recording_id` (newest first, with the
recording_id tiebreaker so same-instant creations still sort deterministically). No
client-selectable sort field - source instructions §22 explicitly permit skipping
this when not required beyond deterministic ordering.

## 11. A pre-existing bug this phase surfaced and fixed

`csc_apps.common.utils.Utils.get_query_params` (ported from PMS, used by every GET
endpoint) never URL-decoded query parameter keys/values - harmless for every filter
value used anywhere in this codebase before Phase 7 (IDs, enum strings, none
containing a space or other reserved character), but `road_name` free-text search
is the first filter that does. Fixed with `urllib.parse.unquote_plus` on both key
and value - a narrow, necessary fix to shared code, not a Phase 7-only workaround
duplicated elsewhere.

## 12. Known limitations / open decisions

- Only `Recording`'s own columns are searchable; no eDAR AI/APPROVED field search.
- No transcript full-text search.
- No crash-date filtering (only recording creation date).
- No configurable sort field.
- No index added on `Recording.created_at`/`road_name`/`case_fir_number` - not
  justified at current scale; revisit if query performance data says otherwise
  (source instructions explicitly warn against speculative indexes).
- REVIEWER/ADMIN's list scope (own recordings only) is narrower than their
  existing detail-endpoint access (any recording by ID) - a real, confirmed product
  decision, not an inconsistency to silently paper over. `docs/open-decisions.md`
  OD-001 (review assignment) remains open; this phase does not add a "recordings
  pending my review" view.
