import uuid

from django.db import models

from csc_apps.recordings.models.recording import Recording


class VoiceSession(models.Model):
    """One transcript-WebSocket conversation session for a Recording
    (docs/phase10-live-voice-agent.md §VoiceSession lifecycle) - one row per
    `WS /recordings/<id>/transcript/` connection. A Recording may have more
    than one - resuming a conversation continues against the same
    Recording/eDAR state rather than starting a new Recording (task §21).

    Deliberately does not model VoiceMessage/VoiceQuestion/VoiceAnswer/
    ConversationTurn as separate rows - `transcript_original`/`transcript_english`
    below are the accumulated running conversation text, and idempotency/audit per
    turn reuses the existing ProcessingEvent model (docs/phase10-live-voice-
    agent.md §Idempotency) rather than a new per-turn table.
    """

    STATUS_CHOICES = [
        ('CREATED', 'Created'),
        ('ACTIVE', 'Active'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('INCOMPLETE', 'Incomplete'),
    ]

    # UUID, not an AutoField - used as an opaque row identifier in
    # ProcessingEvent/ActivityLog metadata; a sequential integer would leak
    # how many voice sessions this deployment has created.
    voice_session_id = models.UUIDField(verbose_name='Voice Session ID', primary_key=True, default=uuid.uuid4, editable=False)
    recording = models.ForeignKey(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='voice_sessions'
    )
    status = models.CharField(verbose_name='Status', choices=STATUS_CHOICES, max_length=15, default='CREATED')

    # Accumulated running conversation transcript (docs/phase10-live-voice-
    # agent.md §Transcript handling) - deliberately NOT stored on the existing
    # Transcript model, whose uniqueness is scoped to an Audio row (Phase 2/3's
    # batch-STT shape); a live voice session has no Audio, and forcing this
    # incremental, many-small-turns shape into that model would fight its own
    # invariant rather than reuse it. `transcript_original` is appended to as raw
    # turns arrive; `transcript_english` is appended to only after translation
    # (identical text if the turn was already English - same identity-skip
    # convention as csc_apps.processing.translation_service).
    transcript_original = models.TextField(verbose_name='Original Transcript', blank=True, default='')
    transcript_english = models.TextField(verbose_name='English Transcript', blank=True, default='')

    created_at = models.DateTimeField(verbose_name='Created At', auto_now_add=True)
    started_at = models.DateTimeField(verbose_name='Started At', null=True, blank=True)
    ended_at = models.DateTimeField(verbose_name='Ended At', null=True, blank=True)

    class Meta:
        db_table = 'voice_sessions'

    def __str__(self) -> str:
        return f'VoiceSession {self.voice_session_id} for Recording #{self.recording_id} ({self.status})'
