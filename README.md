# Crash Scene Co-Pilot

Speech-driven crash-scene reporting: an officer records or uploads audio at the scene,
the system transcribes and translates it, extracts a fixed set of 42 eDAR fields, and
hands the officer a pre-filled record to review and approve.

**Phase 0** built the architecture, domain model, the eDAR schema, and authentication.
**Phase 1** added backend audio upload/ingestion —
[`docs/phase1-audio-ingestion.md`](docs/phase1-audio-ingestion.md). **Phase 2** added
Sarvam speech-to-text — [`docs/phase2-sarvam-stt.md`](docs/phase2-sarvam-stt.md).
**Phase 3** added Sarvam translation and the English transcript —
[`docs/phase3-sarvam-translation.md`](docs/phase3-sarvam-translation.md). **Phase 4**
added Gemini AI eDAR extraction (a candidate, not an approved record) —
[`docs/phase4-gemini-edar-extraction.md`](docs/phase4-gemini-edar-extraction.md). See
[`docs/product-scope.md`](docs/product-scope.md) for exactly what is and isn't built yet;
no officer review, approval, or export exists yet by design.

## Documentation

Start with these, in order:

1. [`docs/pms-reference-analysis.md`](docs/pms-reference-analysis.md) — the engineering
   conventions this codebase follows, and why.
2. [`docs/product-scope.md`](docs/product-scope.md) / [`docs/user-workflow.md`](docs/user-workflow.md) — what the product does.
3. [`docs/architecture-decisions.md`](docs/architecture-decisions.md) — the ADRs.
4. [`docs/domain-model.md`](docs/domain-model.md) — the actual data shape.
5. [`docs/open-decisions.md`](docs/open-decisions.md) — everything still unresolved.

Full index: [`docs/`](docs/), [`architecture/system-architecture-v1/`](architecture/system-architecture-v1/),
[`schemas/edar-schema.json`](schemas/edar-schema.json).

## Project layout

```
csc/            Django settings package (config.py, constants.py, settings.py, urls.py)
csc_apps/       one Django app per domain - common, authentication, activity_log,
                recordings, processing, edar
schemas/        schemas/edar-schema.json - the fixed eDAR field schema
docs/           architecture and process documentation
architecture/   system architecture diagrams/notes
```

Conventions (layering, naming, error handling, response envelope, testing style) are
inherited from the PMS reference project — see `docs/pms-reference-analysis.md` for the
full breakdown rather than re-deriving them from the code.

## Running locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in DB_* and SECRET_KEY
python manage.py migrate
python manage.py test csc_apps
python manage.py runserver
```

Requires a local PostgreSQL instance (`DB_*` in `.env`) — same as PMS. Uploaded audio is
written under `private_storage/` (git-ignored, created on first upload). To actually run
the full pipeline, set `SARVAM_API_KEY` and `GEMINI_API_KEY` in `.env` and run, in order,
`python manage.py process_pending_stt_jobs`, then
`python manage.py process_pending_translation_jobs`, then
`python manage.py process_pending_extraction_jobs` (none wired to a task queue —
`docs/open-decisions.md` OD-003 remains open); without keys, everything else still runs
and the full test suite mocks both providers entirely.

## Contribution rules

Same five rules PMS documents in its own `README copy.md`:

- Naming: `lower_case_variable`, `ALL_CAPS_CONSTANT`, `PascalCaseClass`.
- Functions: single responsibility, short and self-explanatory, avoid nesting.
- Prefer an explicit validation/raise over a broad `try/except` in business logic — the
  one shared exception funnel is `csc_apps.common.common.Common.exception_handler`.
- Annotate argument and return types.
- API docs: `python manage.py runserver`, then `/api/schema/swagger-ui/`.
