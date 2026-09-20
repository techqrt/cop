# User Workflow — Crash Scene Co-Pilot

## 1. End-to-end officer journey

```
1. Login                (email + password)
2. Dashboard             (recording history: in-progress, processing, ready for review, completed)
3. Create Recording       (start a new crash report)
4a. Live Recording   OR   4b. Upload Audio
        \                       /
         \                     /
          v                   v
5. Processing            (STT -> translation -> eDAR extraction; officer can leave the screen)
6. Transcript             (original-language + English transcript, read-only reference)
7. Review eDAR             (AI-filled form, grouped by the 7 eDAR modules, edit any field)
8. Completed Record        (officer approval -> immutable approved record)
9. Recording History       (list/filter/search past recordings by state)
10. Export (later phase)   (generate the file/format eDAR intake expects)
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

### 8. Completed Record
Once the officer approves, the record becomes the Layer-3 human-approved record
(`docs/data-provenance.md`). The original AI output is retained alongside it, not
overwritten — an officer edit changes the approved copy, never the AI's original.

### 9. Recording History
Same list as the Dashboard, with search/filter/sort, following PMS's shared `GetAll`
list-endpoint convention (`docs/pms-reference-analysis.md` §5).

### 10. Export (later phase)
Named in the source brief as a later capability ("Eventually export the completed eDAR
record"). Format and destination are an open decision (`docs/open-decisions.md` OD-006), not
designed in Phase 0.

## 3. What Phase 0 does not build

None of the above screens are implemented in Phase 0 — this document defines the target
workflow so the Phase 0 domain model, state machine, and API architecture are shaped
correctly for Phase 1 to build against, per `docs/product-scope.md` §4.
