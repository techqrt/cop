# Phase 10A — Get All Recordings + Full End-to-End Smoke Test

## 1. `GET /recordings/get_all/` — purpose

A lightweight index of every recording accessible to the authenticated user,
deliberately distinct from the paginated, filterable `GET /recordings/` (Phase
7): no pagination, no query parameters, no per-stage status breakdown. It answers
one question only — "what recordings do I have, and what state is each one in
right now" — for a client that just needs a simple list, not a search UI.

## 2. Authentication

The same Bearer/JWT mechanism as every other endpoint. Unauthenticated: 401.

## 3. Authorization

Identical scoping to `GET /recordings/` (Phase 7/9's documented matrix): every
role — officer, REVIEWER, ADMIN — sees only recordings they personally created.
`Recording.objects.filter(officer=requesting_user)` is applied at the query
level, before anything else; the existing `GET /recordings/<id>/` remains the
place a REVIEWER/ADMIN can reach any recording directly by ID. This was a
deliberate continuation of Phase 9's confirmed decision, not a new one.

## 4. Response fields

```json
{
  "status": true,
  "message": "Recordings retrieved successfully.",
  "data": [
    {"recordingId": 13, "status": "COMPLETED", "createdAt": "2026-09-23T10:15:00.123456+00:00"},
    {"recordingId": 14, "status": "PROCESSING", "createdAt": "2026-09-23T10:20:00.654321+00:00"}
  ]
}
```

`data` is a **plain array** — not `GET /recordings/`'s paginated
`{data, presentPage, totalPage, ...}` object. Exactly three fields per
recording: `recordingId`, `status`, `createdAt`. Deliberately excludes
`roadName`/`caseFirNumber`/`policeStationJurisdiction`/per-stage statuses, even
though `GET /recordings/` already exposes them — adding them here would
duplicate that endpoint and erase the intentional distinction between the two.

## 5. Status source

`status` is `Recording.status` **verbatim** — the one canonical lifecycle field
this system already has (`docs/recording-state-machine.md`:
`CREATED`/`RECORDING`/`UPLOADED`/`PROCESSING`/`READY_FOR_REVIEW`/`IN_REVIEW`/
`COMPLETED`/`FAILED`/`RETRY`). Not derived from `ProcessingJob`/`ProcessingEvent`,
which represent per-stage sub-status (STT/translation/extraction) — that
granularity belongs to `GET /recordings/` and `GET /recordings/<id>/`, not this
endpoint. No new status field or competing status system was introduced.

## 6. Ordering

`-created_at, -recording_id` — newest first, with `recording_id` as a
tiebreaker for same-instant creations. The identical ordering `GET /recordings/`
already uses; no client-controlled sort.

## 7. Performance/query behavior

One query: `Recording.objects.filter(...).order_by(...).values('recording_id',
'status', 'created_at')`. No `ProcessingJob`/`EdarRecord` join at all (unlike
`GET /recordings/`, which needs one for its per-stage status columns) — this
endpoint doesn't expose that, so it doesn't need the extra queries. Verified with
a test comparing query counts at 3 vs. 13 recordings: identical, confirming no
N+1 behavior.

## 8. Security

No transcript, audio, eDAR (AI or APPROVED), provenance, or provider metadata is
in this endpoint's response shape at all — there was never a code path that
could expose them. Verified directly: a recording with a transcript containing a
planted marker string, and another with a planted eDAR field value, both produce
a `get_all` response with neither marker present.

## 9. Smoke-test environment

- Python 3.12.6, Django 5.2.7 (per `requirements.txt`)
- Database: isolated scratch SQLite (Postgres unavailable in this environment,
  same limitation as every prior smoke test in this project)
- Storage: isolated local private-storage root, scoped to this smoke test only
- Sarvam: credentials present and used for real STT and translation calls
- Gemini: credentials present and used for real 3-call extraction
- `DEBUG=True`, `ALLOWED_HOSTS=*` in the project's local `.env` — unchanged from
  Phase 9's finding; still a documented Phase 10 deployment requirement, not
  fixed here (out of this phase's scope)
- Git: working tree includes uncommitted Phases 6–10A changes on top of commit
  `c30d349`

No secrets are reproduced anywhere in this document or the test run's output.

## 10. Test fixture description

Two controlled, synthesized crash-scene statements (macOS `say`, 16kHz mono
WAV), reused from earlier smoke testing, each describing: crash date/time, a
national highway location, road/weather/lighting conditions, a rear-end
collision between a motorcycle and a car, two casualties (rider - grievous
injury, driver - minor injury), no hit-and-run, one witness, and an officer
remark about missing road markings. Deliberately omits: case/FIR number, police
station jurisdiction, vehicle registration numbers, hospital name, and several
infrastructure/officer-assessment fields — to prove Gemini reports them
`UNKNOWN` rather than inventing values.

- **Recording 1:** Hindi (`hi-IN`) — exercises the full STT → real Sarvam
  translation → Gemini path.
- **Recording 2:** English (`en-IN`) — exercises the Phase 3 same-language
  identity path (no Sarvam translation call; English transcript text-identical
  to the original).

## 11. Complete pipeline results

See the report's §10 (Pipeline Results table) for the full stage-by-stage
outcome. Both recordings passed every stage with real providers.

## 12. Database verification

See the report's §14. Full relationship chain confirmed intact for recording 1:
`Recording → Audio → 3×ProcessingJob (STT/TRANSLATION/EXTRACTION, all
SUCCEEDED) → 2×Transcript (ORIGINAL hi-IN, ENGLISH en-IN) → EdarRecord (quality
VALIDATED, review APPROVED) → 54 AI + 54 APPROVED EdarFieldValue rows → 18
ProcessingEvents → ActivityLog entries for login/recording-creation/
approval/export`. Only `AI`/`APPROVED` layers exist — no unexpected layer.

## 13. Authorization verification

A second officer identity was denied at `GET /recordings/<id>/`, `POST
.../edar/approve/`, `GET .../export/`, and was excluded from both `GET
/recordings/get_all/` and `GET /recordings/` for the first officer's recording —
all five checked in the same run. See report §15.

## 14. Export verification

`GET /recordings/<id>/export/` returned the **APPROVED** values for the two
fields corrected during review (`road_name`, `speed_limit_on_road`) — never the
original AI values, one of which (`speed_limit_on_road`) was a real upstream
transcription artifact (Sarvam misheard "साठ" (sixty) as the digit "7"),
corrected by the officer to `60` and confirmed present in the export as `60`,
never `7`. `case_fir_number` (never spoken) correctly exported as `{known:
"UNKNOWN", value: null}`. `gpsCoordinates` correctly `null` (never captured on
this fixture). Vehicle/casualty grouping preserved as two-element arrays each.
Export produced an `ActivityLog` entry (`{event: 'export', recording_id,
edar_record_id}`) with no field values.

## 15. AI vs APPROVED verification

For `speed_limit_on_road` on recording 1:

| Layer | Value |
|---|---|
| AI | `7` |
| APPROVED (officer-corrected) | `60` |
| Export | `60` |

All 54 AI rows were captured before approval and compared byte-for-byte
(field_key, value, confidence, evidence, extraction_version) against the same
54 rows after approval — identical. The export never showed the AI value.

## 16. Known limitations

- Postgres was unavailable; the smoke test ran against SQLite, as every prior
  smoke test in this project has. Behavior is expected to be equivalent (no
  Postgres-specific query construct is used anywhere), but this has not been
  verified against the actual production database engine.
- `speed_limit_on_road`'s AI value being wrong (`7` instead of `60`) is a
  pre-existing Sarvam STT/TTS artifact from the synthesized test audio, already
  documented in earlier smoke testing — not a new defect, and useful here only
  as a realistic worked example for the AI-vs-APPROVED-vs-Export test.
- `GET /recordings/`'s `road_name`/`case_fir_number` filters search
  `Recording`'s own Phase 1 columns, which this smoke test's uploads never
  populated (no `road_name`/`case_fir_number` form field was sent at upload
  time) — a filter-by-road-name search correctly returned empty. Not a defect;
  documented in `docs/phase7-history-search.md` already.
- Two officer identities were tested for authorization (owner, other officer).
  REVIEWER/ADMIN authorization behavior was not re-exercised live in this smoke
  test (it is already covered by the automated test suite, which passed in
  full) - documented as covered by tests, not by live smoke-test evidence, in
  the interest of not spending additional real Gemini/Sarvam quota on scenarios
  the unit/integration suite already proves deterministically.

## 17. Production deployment requirements discovered

None new beyond what Phase 9 already documented: set `ALLOWED_HOSTS` for the
real domain, set `DEBUG=False` on the deployed server, terminate TLS and enable
the TLS-dependent security settings, configure CORS once a frontend origin
exists, verify against the actual Postgres database before go-live.

## 18. Deployment was not performed

No production deployment, infrastructure change, DNS change, TLS installation,
secret rotation, firewall change, monitoring/alerting configuration, or CI/CD
change was made or attempted in this phase. All work was local, isolated
smoke-test validation.
