"""Phase 10 Live Voice Agent - transcript processing core (docs/phase10-live-
voice-agent.md). Called from csc_apps.recordings.consumers.TranscriptConsumer
(the WebSocket endpoint) - this module has no DRF/HTTP/channel dependency of
its own, it is plain, synchronous, ORM-based service-layer logic, matching
this project's existing controller -> view -> service convention (the
consumer plays the "controller+view" role for the WebSocket transport).

Reuses, rather than duplicates, Phase 10B's targeted-extraction machinery
(csc_apps.processing.extraction_service): the same GeminiExtractionProvider,
the same quality_validation.assess_candidate, and the same
merge_resolved_fields_into_ai_layer the async supplemental-audio path uses -
a live transcript turn and a supplemental-audio upload are both, at their
core, "resolve some currently-unknown fields from a new piece of English
text, without ever touching an already-KNOWN field or an approved record."

No STT here at all (task §5/§16) - the transcript already comes from
Flutter/Sarvam's own Voice Agent. Translation only runs when the transcript
isn't already English - reusing the existing Sarvam translation provider
directly and synchronously (not the async, job-queued
csc_apps.processing.translation_service wrapper - a live turn needs its
English text in the same message-handling cycle the caller is waiting on).
"""

from django.db import transaction
from django.utils import timezone

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.quality_validation import STATUS_INVALID, assess_candidate
from csc_apps.edar.schema_loader import load_schema
from csc_apps.processing import event_types
from csc_apps.processing.extraction_service import (
    _entity_context_lines,
    _entry_from_extracted_field,
    _max_entity_index,
    _module_max,
    merge_resolved_fields_into_ai_layer,
)
from csc_apps.processing.models import ProcessingEvent
from csc_apps.processing.providers.base import ProviderError
from csc_apps.processing.providers.gemini.provider import GeminiExtractionProvider
from csc_apps.processing.providers.gemini.schema_adapter import MAX_CASUALTIES, flat_field_keys, repeating_field_keys
from csc_apps.processing.providers.sarvam.translation_provider import SarvamTranslationProvider
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.models.voice_session import VoiceSession
from csc_apps.recordings.state_machine import can_transition, transition

_TARGET_LANGUAGE_CODE = 'en-IN'


class RecordingNotFoundError(ValueError):
    pass


class NotAuthorizedError(ValueError):
    pass


def authorize_recording_for_voice(user: User, recording_id: int) -> Recording:
    """Same authorization rule as csc_apps.recordings.views.RecordingView.
    _get_authorized_recording (owning officer, or REVIEWER/ADMIN) - the
    transcript WebSocket (csc_apps.recordings.consumers.TranscriptConsumer)
    reuses this exact check at connection time, not a separate WebSocket-only
    permission model (task §15/§27)."""
    recording = Recording.objects.select_related('officer').filter(recording_id=recording_id).first()
    if recording is None:
        raise RecordingNotFoundError('Recording not found')
    is_owner = recording.officer_id == user.user_id
    is_privileged = user.role in ('REVIEWER', 'ADMIN')
    if not (is_owner or is_privileged):
        raise NotAuthorizedError('Not allowed to access this recording')
    return recording


def start_voice_session(recording: Recording, user: User) -> VoiceSession:
    """Called once the transcript WebSocket connection is authorized and
    accepted (TranscriptConsumer.connect). One VoiceSession per connection -
    a Recording may accumulate several conversations over time (task §21),
    each continuing against the same Recording/EdarRecord, never creating a
    second Recording."""
    with transaction.atomic():
        voice_session = VoiceSession.objects.create(
            recording=recording, status='ACTIVE', started_at=timezone.now(),
        )
        # RECORDING, not PROCESSING - this Recording has no ProcessingJob at
        # all (docs/recording-state-machine.md's own definition of
        # PROCESSING requires one). RECORDING already meant "live audio
        # capture in progress" for the live-recording upload path; reused
        # here rather than inventing a new state. can_transition guards a
        # resumed conversation against re-entering RECORDING from
        # READY_FOR_REVIEW.
        if can_transition(recording.status, 'RECORDING'):
            transition(recording, 'RECORDING')
        ProcessingEvent.objects.create(
            recording=recording, event_type=event_types.VOICE_SESSION_STARTED,
            metadata={'voice_session_id': str(voice_session.voice_session_id)},
        )
        ActivityLog.record(
            user=user, action='Create', model='VoiceSession',
            details={
                'recording_id': recording.recording_id,
                'voice_session_id': str(voice_session.voice_session_id),
            },
        )
    return voice_session


def end_voice_session(voice_session: VoiceSession) -> None:
    """Called when the transcript WebSocket disconnects
    (TranscriptConsumer.disconnect). A conversation ending with fields still
    unknown is valid (task §20/§22) - never an error, never auto-approved;
    COMPLETED vs INCOMPLETE is purely descriptive of whether anything was
    still missing when the connection closed."""
    edar_record = EdarRecord.objects.filter(recording_id=voice_session.recording_id).first()
    has_missing = True
    if edar_record is not None:
        has_missing = (
            EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI').exclude(known='KNOWN').exists()
        )
    voice_session.status = 'INCOMPLETE' if has_missing else 'COMPLETED'
    voice_session.ended_at = timezone.now()
    voice_session.save(update_fields=['status', 'ended_at'])


def process_transcript(
    voice_session: VoiceSession, interaction_id: str, transcript: str, language_code: str | None,
) -> dict:
    """The transcript -> existing eDAR core adapter (docs/phase10-live-voice-
    agent.md §Processing behavior, §Reuse the existing eDAR core). Returns a
    small, JSON-serializable result dict the WebSocket consumer relays back
    as an acknowledgement - never the eDAR payload or a "next question"
    (task §12: Flutter/Sarvam get the current eDAR state and missingFields
    from the existing GET /recordings/<id>/ endpoint, not from this socket).

    Idempotent per (voice_session, interaction_id) - reuses ProcessingEvent's
    own JSON metadata rather than a new tracking table (task §19); a retried
    interaction_id is acknowledged identically without ever reprocessing."""
    recording = voice_session.recording

    existing_event = ProcessingEvent.objects.filter(
        recording=recording, event_type=event_types.VOICE_TURN_PROCESSED,
        metadata__voice_session_id=str(voice_session.voice_session_id),
        metadata__interaction_id=interaction_id,
    ).first()
    if existing_event is not None:
        return existing_event.metadata['result']

    english_text = _to_english(transcript, language_code)
    edar_record, _ = EdarRecord.objects.get_or_create(recording=recording)

    with transaction.atomic():
        voice_session.transcript_original = _append(voice_session.transcript_original, transcript)

        resolved_keys: list[str] = []
        if english_text.strip():
            voice_session.transcript_english = _append(voice_session.transcript_english, english_text)
            resolved_keys = _resolve_turn(edar_record, english_text)

        if resolved_keys and can_transition(recording.status, 'READY_FOR_REVIEW'):
            # First transcript to actually resolve something is what makes
            # this recording ready for review - the same meaning
            # READY_FOR_REVIEW already carries for the upload path
            # (csc_apps.processing.extraction_service._record_success), not a
            # new state invented for voice.
            transition(recording, 'READY_FOR_REVIEW')

        voice_session.save(update_fields=['transcript_original', 'transcript_english'])

        result = {'resolvedFieldCount': len(resolved_keys), 'resolvedFieldKeys': resolved_keys}

        ProcessingEvent.objects.create(
            recording=recording, event_type=event_types.VOICE_TURN_PROCESSED,
            metadata={
                'voice_session_id': str(voice_session.voice_session_id),
                'interaction_id': interaction_id,
                'resolved_field_count': len(resolved_keys),
                'resolved_field_keys': resolved_keys,
                'result': result,
            },
        )
        if resolved_keys:
            # Value-free audit, same convention as every other AI-layer write
            # in this project (csc_apps.processing.extraction_service,
            # docs/phase10b-supplemental-audio.md) - field keys only, never
            # transcript text or extracted values.
            ActivityLog.record(
                user=recording.officer, action='Update', model='EdarRecord',
                details={
                    'recording_id': recording.recording_id,
                    'voice_session_id': str(voice_session.voice_session_id),
                    'event': 'voice_turn', 'resolved_field_keys': resolved_keys,
                },
            )
    return result


def _append(existing: str, new_text: str) -> str:
    new_text = (new_text or '').strip()
    if not new_text:
        return existing
    return f'{existing}\n{new_text}'.strip() if existing else new_text


def _to_english(transcript: str, language_code: str | None) -> str:
    """A translation failure - or the language simply not being supported -
    is treated as "this turn yields nothing" (empty string), never a hard
    error (task §15's no-fabrication rule extends to input Django couldn't
    understand: it contributes no field resolution, it doesn't fail the
    whole conversation). `language_code` absent is treated as "already
    English" - a deliberate, documented assumption (not "always assume
    Hindi") to avoid mistranslating text that may already be in English
    (docs/phase10-live-voice-agent.md §Translation)."""
    text = transcript or ''
    if not text.strip():
        return ''
    if not language_code or language_code == _TARGET_LANGUAGE_CODE:
        return text
    try:
        result = SarvamTranslationProvider().translate(
            text=text, source_language_code=language_code, target_language_code=_TARGET_LANGUAGE_CODE,
        )
        return result.text
    except ProviderError:
        return ''


def _resolve_turn(edar_record: EdarRecord, english_text: str) -> list[str]:
    """Incremental per-transcript extraction (task §7) - reuses Phase 10B's
    exact targeted-extraction/merge mechanism (csc_apps.processing.
    extraction_service), applied to one transcript message instead of one
    supplemental-audio upload. Covers the 28 flat (non-repeating) eDAR
    fields plus vehicle/casualty (Module D/E) slots once their count is
    known - see _bootstrap_entity_fields."""
    schema = load_schema()
    ai_rows = list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI'))
    if not ai_rows:
        ai_rows = _bootstrap_flat_fields(edar_record, schema)

    new_entity_rows = _bootstrap_entity_fields(edar_record, schema, ai_rows)
    if new_entity_rows:
        ai_rows = ai_rows + new_entity_rows

    eligible_keys = [r.field_key for r in ai_rows if r.known != 'KNOWN']
    if not eligible_keys:
        return []

    vehicle_count = _max_entity_index(ai_rows, 'vehicle')
    casualty_count = _max_entity_index(ai_rows, 'casualty')
    entity_context_lines = _entity_context_lines(ai_rows)

    try:
        result = GeminiExtractionProvider().extract(
            english_text=english_text, schema=schema, target_fields=eligible_keys,
            entity_context_lines=entity_context_lines,
        )
    except ProviderError:
        # This transcript didn't yield a usable extraction - a valid no-op,
        # not an error that should fail the conversation (task §15).
        return []

    # Same hard server-side enforcement boundary as Phase 10B's supplemental-
    # audio path (docs/phase10b-supplemental-audio.md §Server-side
    # enforcement): anything the provider returns outside eligible_keys is
    # discarded before validation ever sees it.
    result.fields = [f for f in result.fields if f.field in eligible_keys]

    entries = [_entry_from_extracted_field(f) for f in result.fields]
    assessment = assess_candidate(
        entries=entries, expected_keys=eligible_keys, transcript_text=english_text,
        schema=schema, vehicle_count=vehicle_count, casualty_count=casualty_count,
    )
    if assessment.report['status'] == STATUS_INVALID:
        # Deterministic validation rejected this transcript's candidate -
        # never persisted (task §15), the conversation simply continues.
        return []

    resolved_rows = [r for r in assessment.rows if r['known'] == 'KNOWN']
    resolved_keys, _approved_race = merge_resolved_fields_into_ai_layer(
        edar_record, resolved_rows, result.extraction_version, assessment.report['warnings'],
    )
    # approved_race is deliberately not surfaced as an error here (task §14):
    # an already-approved record simply stops accepting new voice-resolved
    # fields; the officer review/approval workflow remains authoritative and
    # the conversation isn't interrupted by a hard failure over it.
    return resolved_keys


def _bootstrap_flat_fields(edar_record: EdarRecord, schema: dict) -> list[EdarFieldValue]:
    """First transcript for this EdarRecord - unlike Phase 10B's
    supplemental-audio path, there is no prior full extraction to fill gaps
    in, so the flat 28-field baseline (Module A-minus-GPS/B/C/F/G, the same
    set csc_apps.processing.extraction_service._assess always includes) is
    created here, all UNKNOWN, exactly once."""
    EdarFieldValue.objects.bulk_create([
        EdarFieldValue(edar_record=edar_record, field_key=key, layer='AI', known='UNKNOWN')
        for key in flat_field_keys(schema)
    ])
    return list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI'))


def _bootstrap_entity_fields(
    edar_record: EdarRecord, schema: dict, ai_rows: list[EdarFieldValue],
) -> list[EdarFieldValue]:
    """Creates UNKNOWN rows for vehicle/casualty slots once their count is
    already KNOWN - a slot is never pre-guessed before something establishes
    it exists (same principle Phase 10B's entity matching already follows),
    just using the flat count field itself (number_of_vehicles_involved /
    number_of_persons_involved) as that trigger rather than waiting to
    organically stumble onto a numbered mention. Idempotent (only creates
    rows that don't already exist) and safe to call on every turn - a count
    that becomes KNOWN on turn N makes its entity slots eligible starting
    turn N+1, the same one-turn lag Phase 10B's own entity handling already
    has for a newly-introduced vehicle/casualty."""
    by_key = {r.field_key: r for r in ai_rows}
    new_rows: list[EdarFieldValue] = []

    for count_field_key, entity, cap in (
        ('number_of_vehicles_involved', 'vehicle', _module_max(schema, 'vehicle')),
        ('number_of_persons_involved', 'casualty', MAX_CASUALTIES),
    ):
        count_row = by_key.get(count_field_key)
        if count_row is None or count_row.known != 'KNOWN' or not isinstance(count_row.value, int):
            continue
        slots = min(count_row.value, cap)
        for index in range(1, slots + 1):
            for base_key in repeating_field_keys(schema, entity):
                key = f'{entity}.{index}.{base_key}'
                if key not in by_key:
                    new_rows.append(EdarFieldValue(edar_record=edar_record, field_key=key, layer='AI', known='UNKNOWN'))

    if new_rows:
        EdarFieldValue.objects.bulk_create(new_rows)
    return new_rows
