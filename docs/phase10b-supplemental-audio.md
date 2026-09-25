# Phase 10B — Targeted Supplemental Audio for Missing eDAR Fields

## 1. Purpose

Lets an officer record a short follow-up statement after the original recording's
AI eDAR extraction has already run, specifically to fill in fields that extraction
left `UNKNOWN` — without the officer or client needing to know or name which
fields those are. The backend determines eligibility itself, from the recording's
own current AI state; the client only ever sends audio.

## 2. Endpoint

`PUT /recordings/<int:recording_id>/` — shares its path with the existing
`GET /recordings/<id>/` (docs/phase2-sarvam-stt.md), dispatched via
`RecordingViewController.recording_detail_root`, the same
`@extend_schema(methods=[...])` pattern already established for
`GET|POST /recordings/` (docs/phase7-history-search.md).

**Request:** `multipart/form-data`, exactly one field: `audio`. There is
deliberately **no** `fields`/`target_fields`/`missing_fields` parameter of any
kind — a request carrying one is accepted (extra fields are ignored by the
serializer), but it has no effect on anything; eligibility is computed entirely
server-side.

**Response:** the exact same shape `GET /recordings/<id>/` returns
(`RecordingView._build_detail_response`) — no new response fields were added for
this phase. A supplemental upload's effect only ever becomes visible through
`edar.fields` — a field's `known` flipping from `UNKNOWN` to `KNOWN` on a later
`GET`/`PUT` call, never through a second, competing status field.

## 3. Preconditions (checked in order, before any audio is stored)

1. Authentication (401 if missing/invalid).
2. Authorization — the recording's owning officer, or a REVIEWER/ADMIN
   (`RecordingView._get_authorized_recording`, the same rule `GET`/approve/export
   already use).
3. `Recording.status` must be `READY_FOR_REVIEW` or `IN_REVIEW` — the recording
   must already have a completed AI eDAR extraction and not yet be approved.
4. An `EdarRecord` must exist for the recording.
5. `EdarRecord.review_status` must not be `APPROVED`.
6. At least one AI `EdarFieldValue` row must have `known != 'KNOWN'`. If every
   field the original extraction attempted is already `KNOWN`, the request is
   rejected **before** the audio is validated, stored, or any row is created —
   not accepted and left to resolve nothing.

Any failure above is a `400` via the standard `ValueError` → `Common.exception_handler`
path; none of them create an `Audio`, `ProcessingJob`, or `ProcessingEvent` row.

## 4. What a successful upload does (synchronously, inside the request)

Mirrors Phase 1's own upload flow almost exactly (`RecordingView.upload_extract`):

1. Validates the audio (`validate_audio_upload` — same magic-byte/size checks as
   the original upload).
2. Stores it via `LocalPrivateAudioStorage` (same private, UUID-keyed storage).
3. Creates one `Audio` row: `recording=<this recording>`, **`role='SUPPLEMENTAL'`**.
4. Creates one `ProcessingJob(job_type='STT', audio=<the new Audio>,
   status='PENDING')` — `ProcessingEvent`s (`AUDIO_VALIDATED`, `AUDIO_STORED`,
   `PROCESSING_JOB_CREATED`) recorded exactly as the original upload does.
5. Records one `ActivityLog(action='Create', model='Audio', details={role:
   'SUPPLEMENTAL', eligible_field_count, ...})`.
6. Returns the current `GET`-shaped detail response.

**Nothing beyond this runs synchronously.** No STT, translation, or extraction
call happens inside the request — the same async, job-creation-only pattern the
original upload uses (docs/phase1-audio-ingestion.md §15: "the upload endpoint
must not block on external AI processing"). `Recording.status` is **not**
transitioned by this endpoint at all — the state machine has no
`READY_FOR_REVIEW`/`IN_REVIEW` → `PROCESSING` transition, and none is needed: the
officer review/approval workflow (Phase 6) remains the sole path to `COMPLETED`
regardless of how many eDAR fields are still unresolved when a supplemental
upload lands.

## 5. Model changes (see ADR-024 for the full rationale)

- `Audio.recording`: `OneToOneField` → `ForeignKey` (`related_name='audios'`),
  plus a new `role` choice field (`ORIGINAL`/`SUPPLEMENTAL`, default `ORIGINAL`).
- `Transcript`: new `audio` FK (nullable — no backfill needed, every row this
  system writes going forward sets it); `unique_together` moved from
  `(recording, language)` to `(audio, language)`.
- `ProcessingJob`: new `audio` FK (nullable; unset only for `EXPORT` jobs).
- Migrations: `recordings.0003_alter_transcript_unique_together_audio_role_and_more`,
  `processing.0002_processingjob_audio`. Both purely additive/altering — no data
  migration, no backfill, confirmed by `makemigrations --check` reporting no
  further drift.

`csc_apps.processing.stt_service`/`translation_service` now key every Transcript
lookup and every job-chaining `get_or_create` call off `job.audio` rather than
`recording.audio`/`recording`-only queries — required so a supplemental audio's
STT→TRANSLATION→EXTRACTION chain never collides with (or silently reuses) the
original audio's already-`SUCCEEDED` chain. This is a real, necessary touch to the
Phase 2/3 services, but purely additive: for the original audio's own jobs,
`job.audio` resolves to the exact same single Audio row `recording.audio` always
did, so normal-recording behavior is unchanged (confirmed by the full pre-existing
test suite passing unmodified in behavior, only in fixture wiring — see §9).

## 6. How the backend determines unresolved fields

`csc_apps.processing.extraction_service._run_targeted_extraction`, at the moment
the supplemental `EXTRACTION` job actually runs (not at upload time — see §8 on
races):

```python
ai_rows = EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI')
eligible_keys = [r.field_key for r in ai_rows if r.known != 'KNOWN']
```

This is the **only** source of eligibility anywhere in this feature. `gps_coordinates`
is automatically excluded — it never has an AI `EdarFieldValue` row to begin with
(ADR-012). A repeating field (`vehicle.N.*`/`casualty.N.*`) is only eligible for
an index the original extraction already established (an AI row exists for it) —
this call never invites a *new* vehicle/casualty slot, only fills in a missing
attribute of one already identified.

## 7. Targeted extraction pipeline

Dispatched inside the *same* `run_extraction_job` entry point every extraction job
goes through (`process_pending_extraction_jobs` is unmodified), branching on
`job.audio.role`:

1. **Schema/prompt** — `build_targeted_response_schema`/
   `build_targeted_extraction_prompt` (`csc_apps/processing/providers/gemini/`)
   build a small, ad-hoc Gemini schema/prompt covering only `eligible_keys`,
   reusing the same `_field_wrapper_schema`/`_repeating_item_schema` building
   blocks the normal flat/vehicles/casualties schemas use. A repeating entity's
   array is capped at the **highest eligible index**, never the schema's
   `max_repetitions` — this call can only fill in an existing slot, never create
   one.
2. **Entity context** — `_entity_context_lines` builds short "vehicle 1:
   vehicle_type=motorcycle, ..." lines from every currently-`KNOWN` AI field of a
   repeating entity, passed into the prompt (`build_entity_context_block`) purely
   to help Gemini attribute new information ("the car's registration was...") to
   the correct existing index — never re-reported, never persisted itself.
3. **One Gemini call** — `GeminiExtractionProvider.extract(..., target_fields=,
   entity_context_lines=)`, the *same* provider class/method the normal path
   uses, not a second provider implementation. `extraction_version` is tagged
   with `TARGETED_PROMPT_VERSION` (`targeted-v1`), distinct from the normal
   path's `v2`.
4. **Server-side enforcement** — any field the provider returns outside
   `eligible_keys` is discarded (`result.fields = [f for f in result.fields if
   f.field in eligible_keys]`) *before* validation ever sees it. This is a hard
   boundary, not a prompt instruction: even a provider that ignores the schema
   entirely cannot expand write scope beyond what the backend itself determined.
5. **Validation** — `csc_apps.edar.quality_validation.assess_candidate`, reused
   completely unchanged, called with `expected_keys=eligible_keys` (not the full
   42-field set) so it only builds rows for the fields actually in scope.
6. **Merge, never replace** — for each resulting `KNOWN` row, the *existing*
   AI `EdarFieldValue` row for that `field_key` is updated in place
   (`bulk_update`) — there is no delete-then-recreate anywhere in this path.
   Concretely: an already-`KNOWN` AI field is **never** in `eligible_keys` to
   begin with, so it is never touched; a field outside the eligible set is
   filtered out in step 4; the `APPROVED` layer has no code path into this
   function at all.
7. **Race safety** — `eligible_keys` is recomputed fresh at merge time
   (`select_for_update` plus a `known == 'KNOWN'` recheck immediately before
   writing) so a concurrent supplemental request that resolved an overlapping
   field first is never silently overwritten.
8. **Quality summary refresh** — `_refresh_quality_summary` recomputes
   `EdarRecord.quality_status`/`quality_report` from the *current* full AI row
   set, replacing warnings only for the fields this merge actually touched and
   keeping every other field's existing warnings verbatim — it deliberately does
   **not** re-run full-candidate evidence-traceability validation (that would
   require checking an old field's evidence against the *new* transcript, which
   is wrong). `EdarRecord.source_transcript`/`extraction_job` (record-level
   "primary provenance") are left pointing at the original extraction throughout
   — only the per-field `extraction_version` on the fields actually resolved
   changes.

## 8. Outcomes that are success, not error

- **Zero fields resolved by the provider** — the audio simply didn't say anything
  useful for the eligible fields. The job still succeeds; nothing is fabricated.
- **Zero fields eligible at merge time** (a race: something else resolved
  everything between this job's creation and its run) — the job succeeds
  trivially, without ever calling the provider.
- **Partial resolution** — some but not all eligible fields get resolved; the
  rest simply stay `UNKNOWN`, available for a further supplemental upload.

Only a genuine schema/quality-validation failure (`assess_candidate` returns
`INVALID`) fails the job — non-retryably, exactly like the normal extraction
path's own validation-failure handling — and in that case the AI layer is left
completely untouched (the merge step never runs).

## 9. Repeated supplemental uploads

Fully supported, without limit. Each new supplemental `Audio` gets its own
`ORIGINAL`/`ENGLISH` `Transcript` pair and its own `STT`→`TRANSLATION`→`EXTRACTION`
job chain; each targeted extraction independently recomputes eligibility from
whatever is `UNKNOWN` *at that moment* — so a second upload automatically targets
only what the first one (and the original extraction) left unresolved. Live-
verified end to end (§11) with two sequential supplemental uploads, each
resolving a different, non-overlapping subset of fields.

## 10. Testing

- `csc_apps/recordings/test_phase10b_supplemental_audio.py` (19 tests) — the HTTP
  surface: auth/authorization, all five preconditions, the "no field-list
  parameter" guarantee (a request sending `fields`/`target_fields`/
  `missing_fields` succeeds identically to one that doesn't), async behavior
  (job left `PENDING`, no field resolved inline), multiple sequential uploads,
  response-shape parity with `GET`, `ActivityLog` content, `PATCH`/`DELETE`
  remain `405`.
- `csc_apps/processing/test_phase10b_extraction.py` (14 tests) — the merge
  mechanics directly: only-eligible-fields resolution, already-`KNOWN` AI fields
  never touched, `APPROVED` layer never touched, no second `Recording`/
  `EdarRecord`, provider output outside the eligible set discarded, partial/zero
  resolution as valid outcomes, zero-eligible-at-merge-time skips the provider
  call entirely, `INVALID` extraction fails without touching the AI layer,
  `ProcessingEvent` content, quality-report warning-merge behavior, and a
  same-audio-count regression proving a second supplemental upload only targets
  what the first left unresolved.
- `csc_apps/edar/tests.py` (`GeminiTargetedSchemaAdapterTests`,
  `GeminiTargetedPromptTests`, 10 tests) — pure unit tests of the targeted
  schema/prompt builders: flat-only, repeating-only (`maxItems` capped at the
  highest *requested* index, not the schema max), and mixed field-key subsets;
  `additionalProperties: false` throughout; entity-context inclusion/omission.
- Two existing tests were updated, not weakened: `test_endpoint_is_read_only`
  (processing/test_phase5_extraction.py) renamed and narrowed to `PATCH`/`DELETE`
  only, since `PUT` is now a real, separately-tested operation on this path; two
  Phase 2/9 STT-service test fixtures (`processing/tests.py`,
  `recordings/test_phase9_security.py`) were updated to set `ProcessingJob.audio`
  explicitly, matching what real code now always does.
- Suite size: **431 → 474 tests** (43 new), all passing. `manage.py check`: 0
  issues. `makemigrations --check`: no drift beyond the two intended migrations.
  `spectacular --validate`: 0 errors (pre-existing `JWTAuthentication`-extension
  warnings only, unchanged from every prior phase).

## 11. Live end-to-end smoke test (real Sarvam + Gemini)

Isolated scratch SQLite DB/storage, real `SARVAM_API_KEY`/`GEMINI_API_KEY` from
this project's own `.env`, a fresh `runserver` instance, three synthesized
(macOS `say`) crash-scene statements:

1. **Original** — full crash narrative (date, time, road, vehicles, casualties,
   witness, no-hit-and-run) deliberately omitting the case/FIR number, the police
   station name, and the weather/road-surface condition.
2. **Supplemental #1** — "the case has now been registered as FIR number
   45/2026 at Manesar police station."
3. **Supplemental #2** — "it was raining heavily and the road was wet."

**Result**, driven entirely through the real HTTP API
(`POST /recordings/` → `process_pending_*` commands → `PUT /recordings/<id>/` →
`process_pending_*` again, twice):

| Stage | Outcome |
|---|---|
| Original upload → STT → translation → extraction | 54 AI rows created; `case_fir_number`, `police_station_jurisdiction`, `weather_at_time_of_crash`, `road_surface_condition` (among 33 others) correctly `UNKNOWN` — never fabricated |
| Supplemental #1 (FIR/station) | Resolved `case_fir_number` (`"45/2026"`) **and** `police_station_jurisdiction` (`"Manazar Police Station"`) — both were eligible and both were supported by this one audio; `extraction_version` on these two rows contains `targeted-v1` |
| Supplemental #2 (weather) | Resolved `weather_at_time_of_crash` (`"heavy rain"`) and `road_surface_condition` (`"wet road"`) |
| Untouched fields, both merges | `road_name`, `crash_date`, and every other already-`KNOWN` field byte-identical before/after both merges (verified by comparing the full `GET` field map) |
| Record identity | Exactly 1 `Recording`, 1 `EdarRecord`, 54 AI rows, **0** `APPROVED` rows throughout — no auto-approval, no duplicate record |
| Audio/Transcript/Job structure | 3 `Audio` rows (1 `ORIGINAL` + 2 `SUPPLEMENTAL`), 6 `Transcript` rows (one ORIGINAL/ENGLISH pair per Audio), 3 independent `EXTRACTION` jobs (one per Audio, all `SUCCEEDED`) |
| Audit | 2 `ActivityLog(action='Create', model='Audio', role='SUPPLEMENTAL')` entries; 2 `ActivityLog(action='Update', model='EdarRecord', event='supplemental_extraction')` entries, each listing exactly the field keys that call resolved (`['police_station_jurisdiction', 'case_fir_number']`, then `['road_surface_condition', 'weather_at_time_of_crash']`) |
| Async pattern | Each `PUT` returned immediately with the job left `PENDING`; no field flipped to `KNOWN` until the corresponding `process_pending_extraction_jobs` run |

No secrets are reproduced anywhere in this document or the smoke run's output.

## 12. Adversarial regression pass (post-implementation)

A dedicated adversarial pass, run after the initial implementation and smoke test
above, found and fixed one real logic gap and positively confirmed several other
guarantees the original implementation only asserted in prose:

- **Fixed: approval-during-the-async-window race.** The PUT endpoint's own
  `review_status == 'APPROVED'` check runs at upload time, but the supplemental
  extraction job it queues may not run until well after that (it's picked up by
  `process_pending_extraction_jobs` on its own schedule, with no coordination with
  approval). An officer approving the record while the job sits `PENDING` - or
  even while the job is mid-flight, waiting on the Gemini call - was a real,
  reachable gap: the job would silently merge into the AI layer of an
  already-approved record. Reproduced deterministically first
  (`TargetedExtractionMissingPrerequisiteTests.
  test_approval_between_upload_and_job_run_blocks_the_merge`, initially failing
  with the job wrongly `SUCCEEDED`), then closed with two checks in
  `csc_apps.processing.extraction_service`: an early recheck right after
  `_run_targeted_extraction` fetches the `EdarRecord` (covers the common case -
  approval landing anytime before the job starts), and a second,
  `select_for_update()`-locked recheck at the top of `_record_targeted_success`'s
  transaction, immediately before anything is merged (covers the narrower window
  during the provider call itself; correctness-under-Postgres, a no-op under
  SQLite per Django's own documented behavior). Both paths fail the job
  non-retryably with a new `EXTRACTION_RECORD_ALREADY_APPROVED` error code, added
  to `csc_apps.processing.error_classification.NON_RETRYABLE_ERROR_CODES`, rather
  than silently discarding the result. **Re-verified live** against the real HTTP
  API: uploaded a supplemental audio, advanced it through STT+translation only,
  called the real `POST /recordings/<id>/edar/approve/` endpoint to approve the
  record while the extraction job was still `PENDING`, then ran
  `process_pending_extraction_jobs` - the job failed with
  `EXTRACTION_RECORD_ALREADY_APPROVED` and both the AI and APPROVED layers'
  `case_fir_number` rows were confirmed, both via the API and directly in the
  database, to be exactly `{known: UNKNOWN, value: null}`, unchanged from the
  moment of approval.
- **Confirmed, not just asserted: the field-level concurrent-merge guard actually
  works.** `TargetedExtractionTests.
  test_concurrent_overlapping_merge_does_not_clobber_or_double_report` directly
  drives `_record_targeted_success` twice against the same field with two
  independently-resolved (differently-worded) values, simulating two
  supplemental jobs that both read the field as `UNKNOWN` before either
  committed. The first commit's value survives untouched; the second commit
  correctly detects the field is no longer eligible, discards its own result,
  and honestly reports `resolved_field_count: 0` for that job rather than
  fabricating a claim that it resolved something it didn't.
- **Confirmed: the storage-failure cleanup path is real, not just copied
  comment.** A forced `RuntimeError` from `Audio.objects.create` (after the file
  is already written to storage) was verified to (a) delete the just-written
  orphan file, (b) create zero `Audio`/`ProcessingJob` rows, and (c) return the
  standard `400` envelope - directly exercising the same `try/except Exception:
  storage.delete(...); raise` pattern Phase 1's upload already uses, on this new
  code path specifically, rather than assuming the copy was correct.
- **Confirmed: `DEBUG` mode masking applies correctly to this endpoint.** An
  unexpected (non-`ValueError`) exception during the PUT request returns the
  generic message with `DEBUG=False` and the exception's own text (never a raw
  traceback) with `DEBUG=True` - the same `Common.exception_handler`/
  `Utils.env_exception_handler` behavior every other endpoint already has, now
  directly verified for this one rather than assumed from shared-decorator reuse.
  A genuine domain `ValueError` (e.g. "every field already known") was separately
  confirmed to reach the client verbatim regardless of `DEBUG`, since those
  messages are developer-authored and safe by design.
- **Confirmed: Phase 1's original upload validation defect (docs/phase1-audio-
  ingestion.md, the `TemporaryUploadedFile`/`data.copy()` crash above Django's
  2.5MB in-memory threshold) does not reappear on this new endpoint.** A ~3MB
  supplemental audio, well above that threshold, uploads successfully - the fix
  already applied to `csc/settings.py` (`FILE_UPLOAD_MAX_MEMORY_SIZE`) is a
  global Django setting and correctly covers this endpoint too, not just the
  original one.
- **Confirmed, not newly introduced:** an unclassified, non-`ProviderError`
  exception escaping a provider call (e.g. a transport-level error the SDK
  doesn't wrap) would leave a `ProcessingJob` stuck in `RUNNING` - `run_stt_job`/
  `run_translation_job`/`run_extraction_job` only ever catch `ProviderError`
  around their respective provider calls, and `_RUNNABLE_STATUSES` excludes
  `RUNNING`, so nothing would retry it. This is a pre-existing pattern across all
  of Phase 2/3/4's services, unchanged by Phase 10B's targeted-extraction
  addition (`_run_targeted_extraction` follows the exact same try/except shape as
  the path it's modeled on) - noted here as an audited, not newly created, gap;
  fixing it would mean redesigning error handling across every provider call in
  the pipeline, out of this phase's scope.
- Ordinary validation boundaries re-verified specifically on `PUT
  /recordings/<id>/` (not just assumed from `POST /recordings/`'s own tests):
  missing `audio` field, empty file, wrong `Content-Type`, mislabeled
  `Content-Type` with non-matching magic bytes, oversized file, and a malformed
  `recording_id` path segment - all rejected with `400`/no partial state, none
  producing a `500`.

Regression suite after this pass: **474 → 487 tests** (13 new: 1 approval-race
regression + 1 concurrent-merge simulation + 11 adversarial validation/
cleanup/debug-mode/malformed-ID tests), all passing.
`manage.py check`: 0 issues. `makemigrations --check`: no drift.
`spectacular --validate`: 0 errors, same pre-existing warnings only.

## 13. Known limitations

- A targeted call's repeating-entity handling assumes the supplemental statement
  refers to a vehicle/casualty index already established by the original
  extraction, and matches it via the entity-context summary in the prompt, not a
  hard identifier — a genuinely ambiguous supplemental statement (e.g. two
  vehicles of the same type, no other distinguishing detail) could in principle
  be misattributed by the model to the wrong index. Not exercised by the live
  smoke test (its fixture only needed flat, non-repeating fields); covered at the
  unit level by `GeminiTargetedSchemaAdapterTests`'s index-capping tests, not by
  a live multi-vehicle disambiguation scenario.
- Postgres was unavailable in this environment; the regression suite and smoke
  test both ran against SQLite, consistent with every prior phase's testing in
  this project.
- `select_for_update()` is a no-op on SQLite (Django documents this) — the race-
  safety recheck's correctness under real concurrent load is exercised by the
  application-level "recompute eligibility, recheck `known` immediately before
  writing" logic itself, not by an actual database row lock, since this project
  has never run against Postgres in this session.
