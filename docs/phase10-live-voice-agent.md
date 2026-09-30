# Phase 10 — Live Voice Agent Integration

## 1. What changed

The product's input workflow moves from "record/upload audio, then wait" to a
real-time conversation: Sarvam's Voice Agent runs on the Flutter side and
owns the conversation directly - Django's job is to receive each officer
answer's transcript over a WebSocket, incrementally extract whatever it
supports into the eDAR record using the existing Gemini extraction/validation
core, and expose the current state (including what's still missing) through
the existing `GET /recordings/<id>/` endpoint. Everything built in Phases
1-10B (audio upload, STT, translation, batch extraction, supplemental audio,
officer review/approval, export) is reused unchanged as the system of record
and remains fully functional as a parallel input path.

> This phase's first implementation attempt assumed a different transport
> (Flutter+LiveKit+a Django-issued LiveKit token, Sarvam's Voice Agent joining
> that room, an HTTP "API Tool" callback). That architecture was corrected
> before this version shipped - see ADR-025 (superseded) and ADR-026
> (accepted) for the full history. Nothing described below depends on
> LiveKit; Django has no LiveKit responsibility at all.

```
Flutter (Sarvam Voice Agent runs here directly)
    |
    | HTTP
    v
POST /recordings/            <- creates the Recording (no audio required)
    |
    v
Sarvam Voice Agent asks a question, officer answers
    |
    | transcript
    v
WS /recordings/<id>/transcript/     (Django)
    |
    v
existing Gemini extraction + eDAR validation/provenance/merge (unchanged,
csc_apps.processing / csc_apps.edar)
    |
    v
AI-layer EdarFieldValue rows updated
    |
    v
GET /recordings/<id>/        <- Flutter/Sarvam re-fetch: current eDAR state
    |                            + missingFields (field -> question)
    v
Sarvam Voice Agent picks the next question, asks it
    |
    v
(repeat from officer answer, over the same WebSocket connection)
```

## 2. New/changed endpoints

| Endpoint | Change |
|---|---|
| `POST /recordings/` | `audio` is now optional - omitting it creates a Recording with no `Audio`/`ProcessingJob`, ready for a live-voice conversation |
| `WS /recordings/<recording_id>/transcript/` | **New.** Transcript ingestion - see §4 |
| `GET /recordings/<id>/` | Gained `missingFields` - see §5 |

No other endpoint changed. There is no `POST /recordings/<id>/voice-session/`
and no `POST /voice-sessions/<id>/process/` - those belonged to the
superseded LiveKit-based design (ADR-025) and were removed, not kept
alongside.

### 2.1 `POST /recordings/` — audio now optional

`audio` moved from required to optional on the existing upload endpoint
(`RecordingUploadRequestSerializer`/`UploadRecordingRequest`). Omitting it
creates a `Recording` (`status='CREATED'`) with no `Audio` row and no `STT`
`ProcessingJob` - there is no audio file, the conversation itself will
produce the eDAR data via the transcript WebSocket. Providing `audio` runs
the exact, unmodified Phase 1 upload path. GPS/`road_name`/
`police_station_jurisdiction`/`case_fir_number` are still accepted either
way.

## 3. Authentication over WebSocket

`WS /recordings/<recording_id>/transcript/?token=<jwt>` - the same JWT
`POST /auth/login/` issues, passed as a query parameter rather than an
`Authorization` header. Browsers cannot set a custom header on a WebSocket
handshake (a real, well-known limitation of the browser WebSocket API, which
Flutter Web inherits), so a query parameter is the only universally-portable
option.

Verification reuses `csc_apps.authentication.authentication.
verify_access_token` - the exact function `JWTAuthentication.authenticate`
itself calls for every HTTP endpoint, factored out specifically so there is
one JWT-checking implementation, not two (see ADR-026 §3). Authorization
reuses the identical owner-or-REVIEWER/ADMIN rule every other recording
endpoint applies.

A connection that fails authentication or authorization is closed inside
`connect()`, before `accept()` is ever called, with a distinct custom close
code per failure mode (mirroring the equivalent HTTP status this project
uses elsewhere for the same situation - 4401/4403/4404 for missing-or-
invalid-token / not-authorized / recording-not-found).

**Verified live, not assumed**: because the close happens *before*
`accept()`, this is a rejected WebSocket handshake, not a close frame on an
established connection - and a close code is only a property of the latter.
Driving the real server with a real WebSocket client (`websockets`, via
`daphne`) confirms all four rejection paths currently surface to the client
as a generic HTTP 403 at the handshake level, indistinguishable from each
other - the specific close code passed to `self.close(code=...)` is present
in the ASGI-level close message Channels sends, but is not preserved through
to the client once the handshake itself is rejected. The codes are kept in
the implementation (harmless, and correct per the ASGI spec) but should not
be relied on as a way for Flutter to distinguish *why* a connection was
rejected until/unless verified against the specific ASGI server the real
deployment uses.

## 4. Transcript message contract

Each message the WebSocket receives, one per officer answer:

```json
{
  "interactionId": "turn-17",
  "transcript": "Accident raat ke das baje ke aas paas hua tha.",
  "languageCode": "hi-IN"
}
```

`interactionId` and `transcript` are required; `languageCode` is optional
(absent is treated as "already English" - see §6). The response is a small
acknowledgement only - never the eDAR payload and never a "next question"
(Flutter/Sarvam get both from `GET /recordings/<id>/`, per §8):

```json
{
  "status": true,
  "message": "Transcript processed",
  "data": {
    "interactionId": "turn-17",
    "resolvedFieldCount": 1,
    "resolvedFieldKeys": ["crash_time"]
  }
}
```

A validation failure (missing `interactionId`/`transcript`, or an
unexpected processing error) returns `{"status": false, "message": "...",
"error": [...]}` on the same connection - the connection itself is not torn
down, so the conversation can continue with the next transcript.

## 5. `GET /recordings/<id>/` — `missingFields`

Added as a sibling of `edar` in the existing response, for **every**
recording with an eDAR candidate - upload-driven or voice-driven alike, not
a voice-only addition:

```json
{
  "data": {
    "recordingId": 123,
    "edar": { "...": "..." },
    "missingFields": {
      "weather_at_time_of_crash": "What was the weather at time of crash?",
      "road_condition": "What was the road condition?"
    }
  }
}
```

Computed by one function, `csc_apps.edar.missing_fields.
compute_missing_fields`, reused by nothing else - every currently-`UNKNOWN`
flat canonical field, keyed by `field_key`, mapped to its question, in the
schema's own field-declaration order (deterministic, never left to the
conversational agent to decide "what's next" - Python dict insertion order,
preserved through DRF's JSON rendering). Empty (`{}`, never `null`) whenever
there is no eDAR candidate yet or nothing is missing.

A dict keyed by field_key, not a list of `{field, question}` objects - the
first implementation used a list (a typed DRF list serializer gives
drf-spectacular an explicit OpenAPI shape without relying on JSON object
key-order); changed to a dict on request to match this feature's own literal
contract, since Python dicts (and DRF's JSON rendering of them) already
preserve insertion order, so schema order survives either way.

## 6. Incremental extraction

Reuses Phase 10B's exact targeted-extraction/merge mechanism
(`csc_apps.processing.extraction_service`), applied per transcript message:

1. If `languageCode` is given and isn't already English, the transcript is
   translated via the existing `SarvamTranslationProvider`, called directly
   and synchronously (not the async, job-queued `translation_service`
   wrapper - a live message needs its English text in the same
   handling cycle the WebSocket is waiting on). No `languageCode` at all is
   treated as **already English** - a deliberate, documented assumption
   chosen over "always assume Hindi" specifically to avoid mistranslating
   text that may already be in English.
2. On the first transcript for a Recording (no `EdarFieldValue` rows exist
   yet), the 28 flat (non-repeating) eDAR fields are bootstrapped as
   `UNKNOWN` - there is no prior batch extraction to fill gaps in, unlike
   Phase 10B's supplemental-audio path.
3. Whichever of those 28 fields is not yet `KNOWN` becomes `target_fields`
   for one `GeminiExtractionProvider.extract()` call - the same provider
   class the batch path uses, the same schema/prompt building blocks, the
   same server-side "discard anything outside target_fields" enforcement,
   the same `quality_validation.assess_candidate`.
4. A validated `KNOWN` result is merged into the AI layer via
   `merge_resolved_fields_into_ai_layer` - the exact function Phase 10B's
   async supplemental-audio path also calls (refactored out of
   `_record_targeted_success` specifically so both callers share one
   implementation of "merge new information without ever overwriting an
   already-KNOWN field or writing into an approved record").

**No fabrication**: a transcript whose translation fails, whose provider
call fails, or whose result fails deterministic validation resolves nothing
and is not an error - the connection stays open, the conversation continues
with the same fields still missing.

**Known limitation**: incremental extraction is scoped to the 28 flat
fields. Vehicle/casualty (Module D/E) fields are out of scope for this
phase - matching a vehicle or casualty mentioned in a later transcript to a
stable index with no batch extraction to anchor it is a materially larger
feature than a flat field flipping `UNKNOWN` → `KNOWN`.

## 7. Idempotency

A retried `interactionId` on the same WebSocket connection (same
`VoiceSession`) returns the *exact same* acknowledgement, without ever
reprocessing - no re-translation, no second Gemini call, no second merge
attempt. Reuses the existing `ProcessingEvent` model rather than a new
tracking table: each successfully processed transcript writes one
`ProcessingEvent(event_type='voice_turn_processed', metadata={
voice_session_id, interaction_id, resolved_field_keys, result})`, and
processing's very first step is checking for an existing one with the same
`(voice_session_id, interaction_id)` pair.

## 8. Django does not ask questions

Per the task's own explicit boundary: Django determines *what* is missing
and *a* question for it (§5); Sarvam/Flutter decide *when* and *how* to ask,
and own the entire conversational experience. The WebSocket never returns a
"next question" - only an acknowledgement that a transcript was processed.
Flutter/Sarvam re-fetch `GET /recordings/<id>/` to see the current
`missingFields` and pick what to ask next.

## 9. VoiceSession lifecycle

```
CREATED -> ACTIVE -> COMPLETED | INCOMPLETE
```

One `VoiceSession` row per WebSocket connection (not per Recording) - a
Recording may accumulate several conversations over time, each continuing
against the same `Recording`/`EdarRecord`, never creating a second
Recording.

- `ACTIVE`: set the moment the connection is authorized and accepted.
- `COMPLETED` / `INCOMPLETE`: set on disconnect - `COMPLETED` if every flat
  field is `KNOWN` at that point, `INCOMPLETE` otherwise. Both are valid,
  non-error outcomes - a conversation ending with fields still unknown never
  blocks anything and never auto-approves.
- `transcript_original`/`transcript_english` accumulate every message's text
  across the connection's lifetime, append-only - stored directly on
  `VoiceSession` rather than the existing `Transcript` model, whose
  uniqueness is scoped to an `Audio` row (Phase 2/3's batch-STT shape); a
  live transcript message has no `Audio`, and forcing this incremental,
  many-small-messages shape into that model would fight its own invariant
  rather than reuse it.

## 10. Approval boundary

Unchanged from the rest of the system: voice-resolved fields only ever land
in the `AI` layer. `POST /recordings/<id>/edar/approve/` remains the sole
path to the `APPROVED` layer and `COMPLETED` `Recording` status; nothing in
the voice pipeline writes to `APPROVED` or auto-approves. A transcript
processed against an already-approved `EdarRecord` resolves nothing (the
same `merge_resolved_fields_into_ai_layer` approval-race guard Phase 10B's
supplemental-audio path already relies on) rather than erroring.

## 11. Security

- **No client-provided identity is trusted.** Recording/officer identity for
  the WebSocket comes entirely from the verified JWT and the `recording_id`
  in the URL, never from anything in a transcript message.
- **No secret ever reaches Flutter.** `SARVAM_API_KEY`/`GEMINI_API_KEY` are
  never returned by any response; no new credential was introduced for this
  phase at all - the WebSocket reuses the same officer JWT every other
  endpoint already uses.
- **Logging** follows the existing convention
  (`docs/phase9-security-audit-observability.md`) - no raw transcript
  content, no full eDAR payload, no secret, in any log line,
  `ActivityLog`, or `ProcessingEvent` entry. A processed transcript's audit
  trail carries only `recording_id`, `voice_session_id`, and the resolved
  field *keys* - never values or transcript text (verified directly by a
  test that plants a marker string in a transcript and confirms it never
  appears in the resulting `ActivityLog` entry).
- **Error messages** follow `Common.exception_handler`'s existing debug-
  gated convention (`Utils.env_exception_handler`) - an unexpected
  processing error's detail is only shown when `DEBUG=True`, never a raw
  stack trace or secret.

## 12. Infrastructure this phase adds

Django Channels (`channels==4.3.2`) plus `daphne` (Channels' own reference
ASGI server, required to even import `channels.testing.
WebsocketCommunicator` - verified directly, not optional) - the first
WebSocket/ASGI infrastructure in this project. An in-memory channel layer is
configured (`CHANNEL_LAYERS` in `csc/settings.py`) - sufficient since this
endpoint never broadcasts across consumers or processes; a real multi-
process/multi-machine deployment needing cross-process delivery would need
`channels-redis` instead, documented here as an open deployment decision,
not solved in this phase. `csc/asgi.py` now routes `http` to the same
Django application WSGI already serves (unchanged) and `websocket` to
`csc_apps.recordings.routing.websocket_urlpatterns` (new). Serving this
endpoint outside of tests requires an ASGI server (`daphne
csc.asgi:application` or equivalent) - `manage.py runserver` alone still
only serves WSGI/HTTP.

## 13. Testing

- `csc_apps/recordings/test_phase10_live_voice.py` (32 tests):
  `RecordingLiveVoiceCreationAPITests` (audio-optional upload, existing path
  unaffected), `MissingFieldsGetResponseAPITests` (empty/populated/complete
  states, schema-order, real field names), `TranscriptWebSocketTests`
  (authentication including missing/invalid token, authorization for
  owner/REVIEWER/other-officer/unknown-recording, connection lifecycle
  including session resumption, message validation, incremental extraction
  reusing the existing Gemini provider, already-KNOWN protection,
  translation and its identity-skip/failure paths, idempotency, approval
  boundary, session completion, transcript accumulation, and audit
  content/security) - driven via Channels' own `WebsocketCommunicator`
  against the real ASGI application, not a hand-rolled consumer stub.
- **A real, documented testing gotcha**: `TranscriptWebSocketTests` uses
  `django.test.TransactionTestCase`, not `TestCase` - the consumer's ORM
  calls run on a different thread than the test method (Channels' sync-
  consumer dispatch), and `TestCase`'s outer-transaction-per-test wrapping
  deadlocks against that on SQLite (`"database table is locked"`) -
  reproduced directly before applying the fix, not assumed.
- Suite size: **487 → 519 tests** (32 new), all passing. One pre-existing
  test was updated (`test_response_follows_pms_envelope_with_translation_
  fields` in `csc_apps/recordings/tests.py`) to include `missingFields` in
  its expected response-key set - a real, deliberate shape change, not a
  weakened assertion. `manage.py check`: 0 issues. `makemigrations --check`:
  no drift beyond the two intended migrations (`recordings.0004_voicesession`,
  `recordings.0005_remove_voicesession_room_name`). `spectacular --validate`:
  0 errors (same pre-existing `JWTAuthentication`-extension warnings as
  every prior phase; the WebSocket endpoint itself has no OpenAPI schema
  entry, since it isn't a DRF view - documented here instead, per task §22's
  "if WebSocket documentation is handled separately, follow that pattern").

## 14. Real-provider smoke test

Real `SARVAM_API_KEY`/`GEMINI_API_KEY` were available in this environment.
No live Flutter/Sarvam Voice Agent client was available to drive the
WebSocket exactly as a real deployment would; the transcript-processing
core was instead exercised directly and completely, live, through the real
HTTP `GET`/`POST /recordings/` endpoints plus a real WebSocket connection to
the running server, using real Sarvam translation and real Gemini targeted
extraction - see the final implementation report for the full run and
results.

## 15. Scope explicitly not built

No analytics/dashboards/notifications, no PDF/CSV export, no new review-
assignment system, no new authentication system, no new eDAR schema, no new
approval mechanism, no new AI provider beyond the existing Sarvam/Gemini, no
mobile app code, no telephony integration, no generalized conversation
platform, no LiveKit integration of any kind, and no Sarvam Agent/LiveKit
Agent hosted inside Django - all confirmed out of scope by the corrected
architecture (ADR-026). The existing audio upload/STT pipeline (Phases
1-10B) was not touched in behavior and was not removed - it remains fully
functional as a parallel input path.
