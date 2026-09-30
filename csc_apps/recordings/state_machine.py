"""Recording lifecycle transitions (docs/recording-state-machine.md §3). The legal
transition table is data, not scattered `if` statements - a single lookup plus two
small functions, per PMS's single-responsibility/short-function convention
(docs/pms-reference-analysis.md §12).
"""

from csc_apps.recordings.models.recording import Recording

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    'CREATED': {'RECORDING', 'UPLOADED'},
    # RECORDING -> READY_FOR_REVIEW added in Phase 10 (docs/phase10-live-voice-
    # agent.md §Recording state machine): a live-voice Recording never produces
    # an Audio file, so it never reaches UPLOADED - the only pre-existing edge
    # out of RECORDING. Its first successful voice-turn extraction transitions it
    # straight to READY_FOR_REVIEW, the same state Gemini extraction succeeding
    # already puts an uploaded Recording into (csc_apps.processing.
    # extraction_service._record_success). UPLOADED is kept too, unchanged, for
    # any live-recording flow that still ends in a batch audio upload.
    'RECORDING': {'UPLOADED', 'READY_FOR_REVIEW'},
    'UPLOADED': {'PROCESSING'},
    'PROCESSING': {'READY_FOR_REVIEW', 'FAILED'},
    'READY_FOR_REVIEW': {'IN_REVIEW'},
    'IN_REVIEW': {'COMPLETED', 'READY_FOR_REVIEW'},
    'FAILED': {'RETRY'},
    'RETRY': {'PROCESSING'},
    'COMPLETED': set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def transition(recording: Recording, to_state: str) -> Recording:
    """Raises ValueError for an illegal transition - caught by
    csc_apps.common.common.Common.exception_handler at the API boundary, same as any
    other domain validation failure (docs/pms-reference-analysis.md §8)."""
    if not can_transition(recording.status, to_state):
        raise ValueError(f'Cannot transition Recording from {recording.status} to {to_state}')

    from_state = recording.status
    recording.status = to_state
    recording.save(update_fields=['status', 'updated_at'])

    # Local import: csc_apps.processing depends on csc_apps.recordings (FK to
    # Recording), so the reverse reference stays inside the function body rather
    # than a module-level import, avoiding a cross-app import cycle at load time.
    from csc_apps.processing.models import ProcessingEvent

    ProcessingEvent.objects.create(
        recording=recording,
        event_type=f'recording_transitioned_{from_state}_to_{to_state}'.lower(),
        metadata={'from_state': from_state, 'to_state': to_state},
    )
    return recording
