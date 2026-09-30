# User Workflow — Crash Scene Co-Pilot

## 1. End-to-end officer journey

```
1. Login                  (email + password)
2. Dashboard               (recording history: in-progress, processing, ready for review, completed)
3. Create Recording         (start a new crash report)
4a. Live Recording  OR  4b. Upload Audio  OR  4c. Live Voice Conversation (Phase 10)
        \                    |                    /
         \                   |                   /
          v                  v                  v
5. Processing              (STT -> translation -> eDAR extraction; officer can leave the screen)
                             (4c instead talks to a voice agent running on the device itself -
                              see 4c's own note below; eDAR fields fill in turn by turn over a
                              WebSocket rather than after one batch STT/translation/extraction pass)
6. Transcript               (original-language + English transcript, read-only reference)
7. Review eDAR               (AI-filled form, grouped by the 7 eDAR modules, edit any field)
7a. Add Supplemental Audio   (optional, repeatable - record/upload a short follow-up statement
                              for whatever the AI left unknown; officer never picks which
                              fields, the backend works that out itself - loops back into
                              step 5's async processing for just that follow-up, then
                              returns to step 7 with any newly-resolved fields filled in)
8. Completed Record          (officer approval -> immutable approved record)
9. Recording History         (list/filter/search past recordings by state)
10. Export                   (generate the file/format eDAR intake expects)
```

## 2. Screen-by-screen notes

### 1. Login
Email + password. On success, receives a token the client stores and sends as
`Authorization: Bearer <token>` (mirrors PMS's `JWTAuthentication` pattern — see
`docs/security-baseline.md`).

### 2. Dashboard / Recording History
Lists the officer's own recordings (and, for a Reviewer/Supervisor role, recordings pending
their review — see OD-001), each tagged with its current lifecycle state
(`docs/recording-state-machine.md`). This is the same screen conceptually as item 9; listed
twice because it's both the landing screen and a dedicated history view with filtering.

### 3. Create Recording
Officer starts a new report. The client captures GPS coordinates at this point (device
capture, not AI-derived — see `docs/domain-model.md` §GPS) and the officer chooses live
recording or file upload.

### 4a. Live Recording
Client streams/batches audio to the server; the officer sees a near-real-time transcript
appear (few-second latency acceptable). The officer can stop and either discard or submit.
The **original audio** is preserved as it is produced — it is never derived-and-discarded.

### 4b. Upload Audio
Officer selects a previously recorded audio file. Once uploaded, it enters the same
downstream pipeline as a live recording (SOURCE REQUIREMENT: "the uploaded audio must enter
the same downstream processing pipeline as live recordings after the audio is available.").

### 4c. Live Voice Conversation (Phase 10)
Instead of recording/uploading a single audio file, the officer starts a real-time
conversation: Sarvam's Voice Agent runs directly on the Flutter side and asks
questions out loud, and each answer's transcript is sent to Django over a
WebSocket (`WS /recordings/<id>/transcript/`), which extracts whatever it
supports - reusing the exact same Gemini extraction/validation core as every
other input path (`docs/phase10-live-voice-agent.md`). Django never decides when
or how a question is asked - only what is still missing, exposed to Flutter/the
agent through the same `GET /recordings/<id>/` response every other path already
uses (a new `missingFields` field-key-to-question map). The officer can end the conversation at any
point; fields still unknown simply stay unknown, exactly like an unedited AI field
from any other input path - review/approval (step 8) works identically regardless
of which path produced the eDAR data.

### 5. Processing
Non-blocking: STT, translation, and eDAR extraction run asynchronously
(`docs/processing-pipeline.md`). The officer does not have to wait on this screen; the
recording's state advances in the background and is visible from the Dashboard/History.

### 6. Transcript
Read-only view of the original-language transcript and its English translation. This is the
Layer-2 (AI interpretation) evidence trail the officer can check the extraction against — it
is never the thing the officer edits directly.

### 7. Review eDAR
The AI-extracted eDAR record, grouped by the 7 modules (A–G) from `eDAR Fields.pdf`, each
field showing its value, confidence, and (where useful) the transcript segment it came from.
Fields with no supporting evidence are shown as explicitly unknown, never silently blank
(`docs/unknown-data-policy.md`) — the officer fills those in themselves rather than the AI
guessing. Vehicle and Casualty are repeating sub-sections (max 3 vehicles; casualties
unbounded).

### 7a. Add Supplemental Audio (Phase 10B)
Instead of (or before) typing a missing field in by hand, the officer can record or upload a
short follow-up statement — e.g. "the case number is FIR 45/2026" — and submit it against the
same recording (`PUT /recordings/<id>/`, `docs/phase10b-supplemental-audio.md`). The client
sends audio only; it never tells the server which field it's for — the server works that out
itself from whatever the AI candidate currently has marked unknown, runs the same STT ->
translation -> extraction pipeline as step 5 against just that follow-up, and merges anything
it resolves into the AI-filled form from step 7 (an already-filled field, or the officer's own
approved edits, are never touched). This is optional and repeatable — an officer can send
several short follow-ups over time, each one only ever filling in whatever is still unknown at
that moment — and it is never required before approval: a field can always be filled in by hand
instead, or simply left unknown and approved as such.

### 8. Completed Record
Once the officer approves, the record becomes the Layer-3 human-approved record
(`docs/data-provenance.md`). The original AI output is retained alongside it, not
overwritten — an officer edit changes the approved copy, never the AI's original.

### 9. Recording History
Same list as the Dashboard, with search/filter/sort, following PMS's shared `GetAll`
list-endpoint convention (`docs/pms-reference-analysis.md` §5).

### 10. Export
Named in the source brief as a later capability ("Eventually export the completed eDAR
record") — implemented in Phase 8 (`GET /recordings/<id>/export/`,
`docs/phase8-export.md`) as a JSON export of the `APPROVED` layer only, grouped by the 7
eDAR modules; rejected until the record is approved. The destination system that
consumes this export (eDAR intake) remains outside this project's scope
(`docs/open-decisions.md` OD-006) — this screen only produces the document, it does not
transmit it anywhere.

## 3. Implementation status

This document described the target workflow before any of it was built (Phase 0), so the
domain model, state machine, and API architecture could be shaped correctly for later
phases to build against (`docs/product-scope.md` §4). As of this revision, every screen
above is implemented end to end:

| Screen | Endpoint(s) | Phase |
|---|---|---|
| 1. Login | `POST /auth/login/` | 0 |
| 2/9. Dashboard / History | `GET /recordings/get_all/` (lightweight), `GET /recordings/` (filtered/paginated) | 10A, 7 |
| 3/4a/4b. Create Recording | `POST /recordings/` (`audio` optional as of Phase 10) | 1, 10 |
| 4c. Live Voice Conversation | `WS /recordings/<id>/transcript/`, plus `missingFields` on `GET /recordings/<id>/` | 10 |
| 5. Processing | (async — `process_pending_stt_jobs`/`process_pending_translation_jobs`/`process_pending_extraction_jobs`, polled via `GET /recordings/<id>/`; 4c's path is synchronous per-turn instead) | 2, 3, 4 |
| 6/7. Transcript / Review eDAR | `GET /recordings/<id>/` | 2, 3, 4, 5 |
| 7a. Add Supplemental Audio | `PUT /recordings/<id>/` | 10B |
| 8. Completed Record | `POST /recordings/<id>/edar/approve/` | 6 |
| 10. Export | `GET /recordings/<id>/export/` | 8 |

No screen listed above remains unbuilt. `docs/api-architecture.md` §1 has the
authoritative, current endpoint table (including request/response shapes, response
envelope, and authorization rules) — this document stays the narrative/flow reference,
not the contract of record for a specific request/response shape.
