# API Architecture

Phase 0 defined API **domains and conventions**; every phase since has either extended
the one `GET /recordings/<id>/` detail endpoint or added exactly one new endpoint of its
own (§1's table has the full, current list through Phase 10B). This mirrors PMS's own
request/response conventions (`docs/pms-reference-analysis.md` §§3, 5, 6) so a shared
frontend pattern works across both systems.

## 1. Domain organization

One Django app (`csc_apps/<domain>/`) per API domain, each with its own `urls.py` mounted
under a path prefix in `csc/urls.py` — the same pattern as PMS's `pms/urls.py`.

| Domain | Path prefix | App | Status |
|---|---|---|---|
| Authentication | `/auth/` | `csc_apps.authentication` | Implemented (login) |
| Recordings (upload) | `POST /recordings/` | `csc_apps.recordings` | Implemented — Phase 1, `docs/phase1-audio-ingestion.md` |
| Recordings (detail/transcripts/eDAR) | `GET /recordings/<id>/` | `csc_apps.recordings` | Implemented — Phase 2 (`docs/phase2-sarvam-stt.md` §7), extended Phase 3 with the English transcript (`docs/phase3-sarvam-translation.md` §11), extended Phase 4 with the AI eDAR candidate (`docs/phase4-gemini-edar-extraction.md` §15), extended Phase 5 with quality report, per-field evidence/confidence and structured validation issues (`docs/phase5-validation-provenance.md` §12) — no new endpoint |
| Recordings (eDAR approval) | `POST /recordings/<id>/edar/approve/` | `csc_apps.recordings` | Implemented — Phase 6 (`docs/phase6-officer-review-approval.md`) — creates the APPROVED eDAR snapshot; AI rows are never modified |
| Recordings (history/search) | `GET /recordings/` | `csc_apps.recordings` | Implemented — Phase 7 (`docs/phase7-history-search.md`) — shares its path with `POST /recordings/`; scoped to the requester's own recordings |
| Recordings (approved eDAR export) | `GET /recordings/<id>/export/` | `csc_apps.recordings` | Implemented — Phase 8 (`docs/phase8-export.md`) — JSON export of the APPROVED layer only; rejected until approved |
| Recordings (lightweight index) | `GET /recordings/get_all/` | `csc_apps.recordings` | Implemented — Phase 10A (`docs/phase10a-get-all-and-smoke-test.md`) — unpaginated `{recordingId, status, createdAt}` list, own recordings only |
| Recordings (supplemental audio) | `PUT /recordings/<id>/` | `csc_apps.recordings` | Implemented — Phase 10B (`docs/phase10b-supplemental-audio.md`, ADR-024) — shares its path with `GET /recordings/<id>/`; audio-only request, server determines missing eDAR fields itself and merges any it resolves into the AI layer |
| Audio (raw retrieval) | `/recordings/<id>/audio/` | `csc_apps.recordings` | Deferred (OD-012) |
| Processing | `/recordings/<id>/processing/` | `csc_apps.processing` | Superseded — folded into the combined detail endpoint above rather than built as its own sub-resource (docs/phase2-sarvam-stt.md §7) |
| eDAR | `/recordings/<id>/edar/` | `csc_apps.edar` | Superseded — AI-candidate eDAR data folded into the combined detail endpoint above (docs/phase4-gemini-edar-extraction.md §15); officer editing was ultimately built as part of approval (`POST /recordings/<id>/edar/approve/`, Phase 6), not a separate editing endpoint |
| Review | `/recordings/<id>/edar/review/` | `csc_apps.edar` | Superseded — no dedicated "start review" action exists; `IN_REVIEW` is a transient internal hop the approval action itself walks through (docs/recording-state-machine.md, Phase 6) |

Two rows present in this table through Phase 7 (`Export` at a placeholder
`/recordings/<id>/export/`, `History` at a placeholder separate path) have been
removed as of this revision — both are now **Implemented**, at the exact paths
shown in the table above (Phase 7's `GET /recordings/` and Phase 8's
`GET /recordings/<id>/export/`), so a separate "not modeled" placeholder row for
either would now just contradict the real row above it.

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
enforced by `JWTAuthentication` (`csc_apps/authentication/authentication.py`). The
per-endpoint role matrix (which of Officer/Reviewer/Admin may do what) is now fully built
out and documented in `docs/security-baseline.md`'s authorization matrix — in short: a
recording's owning officer or any Reviewer/Admin may view, approve, export, or send
supplemental audio for it (`RecordingView._get_authorized_recording`, one shared check
reused by `GET`/`PUT`/approve/export); every role is scoped to only their own recordings
in a *list* context (`GET /recordings/`, `GET /recordings/get_all/`), even Reviewer/Admin
(docs/phase9-security-audit-observability.md §4).

## 4. Error conventions

Same funnel-through-one-decorator pattern as PMS (`docs/pms-reference-analysis.md` §8):
`ValueError`/custom `ValidationErrors` -> 400, custom `TokenErrors`/JWT errors -> 401,
generic `Exception` -> 400 with message redacted outside debug (verified directly, not
just assumed, for every endpoint below including Phase 10B's `PUT` — docs/phase10b-
supplemental-audio.md §12). New for CSC: processing-job errors are never surfaced
synchronously (there's no synchronous call to fail) — a failed `ProcessingJob` is
visible via `GET /recordings/<id>/` (`processingStatus`/`translationStatus`/
`extractionStatus`, `*FailureReason`, `extractionIssues`), not via an HTTP error on
some other request. See `docs/error-retry-strategy.md`.

## 5. Asynchronous job behavior

As actually built (Phase 1 onward — corrected from this section's original Phase 0
placeholder URLs, which described `POST /recordings/<id>/audio/` and
`GET /recordings/<id>/processing/`, neither of which was ultimately built at those
paths): `POST /recordings/` (upload) and `PUT /recordings/<id>/` (Phase 10B
supplemental audio) both return as soon as a `ProcessingJob` is enqueued — neither
ever blocks on STT/translation/extraction. Callers poll the same
`GET /recordings/<id>/` this table already lists to observe job status
(`processingStatus`/`translationStatus`/`extractionStatus`); no separate
`/processing/` sub-resource or webhook/notification mechanism was built. A pipeline
stage is picked up and actually run by a `process_pending_<stage>_jobs` management
command (`docs/processing-pipeline.md` §3, `docs/error-retry-strategy.md`), not by
the request that created the job.

## 6. What is implemented vs. documented

Implemented, end to end: `/auth/login/` (Phase 0); `POST /recordings/` audio
ingestion (Phase 1); `GET /recordings/<id>/` processing status, both transcript
versions, and the AI eDAR candidate with quality/provenance (Phase 2 original
transcript, Phase 3 English transcript, Phase 4 eDAR, Phase 5 validation);
`POST /recordings/<id>/edar/approve/` officer review + approval, creating the
immutable `APPROVED` layer (Phase 6); `GET /recordings/` history/search (Phase 7);
`GET /recordings/<id>/export/` approved-eDAR export (Phase 8); Phase 9 security/
audit hardening across all of the above (no new endpoint);
`GET /recordings/get_all/` lightweight recording index (Phase 10A);
`PUT /recordings/<id>/` targeted supplemental audio, merged into the AI layer
without a client-supplied field list (Phase 10B). `GET /recordings/<id>/` and
`POST /recordings/<id>/edar/approve/` share one response shape
(`RecordingView._build_detail_response`), and `PUT /recordings/<id>/` reuses that
same shape rather than inventing a fourth. Documented only, not built: `Audio`
(raw-file retrieval, OD-012).
