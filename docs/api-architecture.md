# API Architecture

Phase 0 defined API **domains and conventions**; Phase 1 added audio ingestion; Phase 2
added the recording-detail/transcript endpoint (`docs/phase1-audio-ingestion.md`,
`docs/phase2-sarvam-stt.md`). This mirrors PMS's own request/response conventions
(`docs/pms-reference-analysis.md` §§3, 5, 6) so a shared frontend pattern works across both
systems.

## 1. Domain organization

One Django app (`csc_apps/<domain>/`) per API domain, each with its own `urls.py` mounted
under a path prefix in `csc/urls.py` — the same pattern as PMS's `pms/urls.py`.

| Domain | Path prefix | App | Status |
|---|---|---|---|
| Authentication | `/auth/` | `csc_apps.authentication` | Implemented (login) |
| Recordings (upload) | `POST /recordings/` | `csc_apps.recordings` | Implemented — Phase 1, `docs/phase1-audio-ingestion.md` |
| Recordings (detail/transcripts/eDAR) | `GET /recordings/<id>/` | `csc_apps.recordings` | Implemented — Phase 2 (`docs/phase2-sarvam-stt.md` §7), extended Phase 3 with the English transcript (`docs/phase3-sarvam-translation.md` §11), extended Phase 4 with the AI eDAR candidate (`docs/phase4-gemini-edar-extraction.md` §15) |
| Audio (raw retrieval) | `/recordings/<id>/audio/` | `csc_apps.recordings` | Deferred (OD-012) |
| Processing | `/recordings/<id>/processing/` | `csc_apps.processing` | Superseded — folded into the combined detail endpoint above rather than built as its own sub-resource (docs/phase2-sarvam-stt.md §7) |
| eDAR | `/recordings/<id>/edar/` | `csc_apps.edar` | Superseded — AI-candidate eDAR data folded into the combined detail endpoint above (docs/phase4-gemini-edar-extraction.md §15); a dedicated eDAR endpoint may still be added in a later phase for officer editing |
| Review | `/recordings/<id>/edar/review/` | `csc_apps.edar` | Not modeled (docs/domain-model.md §4) - Phase 6 |
| Export | `/recordings/<id>/export/` | — | Not modeled (OD-006) |
| History | `/recordings/` (GET, list) | `csc_apps.recordings` | Not modeled |

Nesting audio/processing/edar under `/recordings/<id>/...` (rather than top-level
collections) reflects that every one of those resources belongs to exactly one Recording
and is meaningless without it — this is an implementation decision, not sourced from the
brief, made for URL clarity. The originally-sketched separate `Transcripts` row was folded
into the `GET /recordings/<id>/` detail response in Phase 2 rather than built as its own
endpoint — a documented simplification (`docs/phase2-sarvam-stt.md` §7), not a silent
scope change: the transcript is still exactly Phase 0's `Transcript` model, just returned
alongside processing status instead of at its own URL.

## 2. Request/response conventions (adopted from PMS verbatim)

- Response envelope: `{"status": true, "message": ..., "data": ...}` on success,
  `{"status": false, "message": ..., "error": [...]}` on failure
  (`docs/pms-reference-analysis.md` §6).
- Request bodies: plain DRF `Serializer` (not `ModelSerializer`) validating into a typed
  `dataclasses.dataclass`, attached to `request.params` by a shared validation decorator.
- List endpoints: the shared `GetAll` dataclass/serializer (`values`, `page_num`, `limit`,
  `sort_by`, `sort_order`, `filter_key`, `filter_value`, `search_key`, `from_date`,
  `to_date`), paginated via `Utils.add_page_parameter`.
- OpenAPI schema: `drf-spectacular`, `@extend_schema` on every controller method, same as
  PMS — `/api/schema/`, `/api/schema/swagger-ui/`, `/api/schema/redoc/`.

## 3. Authentication & authorization requirements

Every endpoint except `/auth/login/` requires a valid `Authorization: Bearer <token>` header,
enforced by `JWTAuthentication` (`docs/security-baseline.md`). Role-based authorization
(Officer/Reviewer/Admin) gates specific actions (e.g. only Reviewer/Admin may approve an
`EdarRecord`) — the exact per-endpoint role matrix is Phase 1 (endpoints don't exist yet to
gate), but the mechanism (role claim in the JWT payload, checked per-request) is defined now
in `csc_apps/authentication/authentication.py`.

## 4. Error conventions

Same funnel-through-one-decorator pattern as PMS (`docs/pms-reference-analysis.md` §8):
`ValueError`/custom `ValidationErrors` -> 400, custom `TokenErrors`/JWT errors -> 401,
generic `Exception` -> 400 with message redacted outside debug. New for CSC: processing-job
errors are never surfaced synchronously (there's no synchronous call to fail) — a failed
`ProcessingJob` is visible via `GET /recordings/<id>/processing/` returning its `status` and
`error_code`, not via an HTTP error on some other request. See
`docs/error-retry-strategy.md`.

## 5. Asynchronous job behavior

Endpoints that trigger pipeline stages (Phase 1: e.g. `POST /recordings/<id>/audio/` once
upload completes) return **immediately** with `202`-style semantics once a `ProcessingJob`
is enqueued (`docs/processing-pipeline.md` §3) — they do not block on STT/translation/
extraction. Callers poll `GET /recordings/<id>/processing/` (or a future webhook/notification
mechanism — not decided, not needed for Phase 0) to observe job status.

## 6. What is implemented vs. documented

Implemented: `/auth/login/` (Phase 0), `POST /recordings/` audio ingestion (Phase 1), and
`GET /recordings/<id>/` processing status + both transcript versions + AI eDAR candidate
(Phase 2 original transcript, Phase 3 English transcript, Phase 4 eDAR) — all controller
-> view -> model, all using the shared response-envelope utilities. Zero new public
endpoints across Phases 2-4 — each phase extended the one existing detail endpoint rather
than adding its own. Documented only: every other endpoint listed above — deliberately not
built yet, so this document remains the contract later phases build against.
