"""Named ProcessingEvent.event_type constants for the audio-ingestion stage
(docs/observability.md §2, docs/phase1-audio-ingestion.md). Phase 0 deliberately left
the full event vocabulary open until a concrete stage existed to name
(docs/observability.md §4) - this is that first concrete stage.

Recording state transitions (CREATED->UPLOADED->PROCESSING) already get their own
generic `recording_transitioned_<from>_to_<to>` event from
csc_apps.recordings.state_machine.transition() - the constants below are the
additional, more specific milestones inside the ingestion request that aren't
represented by a state change on their own.
"""

RECORDING_CREATED = 'recording_created'
AUDIO_VALIDATED = 'audio_validated'
AUDIO_STORED = 'audio_stored'
PROCESSING_JOB_CREATED = 'processing_job_created'

# Phase 2 - Sarvam STT (docs/phase2-sarvam-stt.md §Processing events).
STT_STARTED = 'stt_started'
STT_SUCCEEDED = 'stt_succeeded'
STT_FAILED = 'stt_failed'

# Phase 3 - Sarvam Translation (docs/phase3-sarvam-translation.md §Processing events).
TRANSLATION_JOB_CREATED = 'translation_job_created'
TRANSLATION_STARTED = 'translation_started'
TRANSLATION_SUCCEEDED = 'translation_succeeded'
TRANSLATION_FAILED = 'translation_failed'

# Phase 4 - Gemini eDAR extraction (docs/phase4-gemini-edar-extraction.md
# §Processing events).
EXTRACTION_JOB_CREATED = 'extraction_job_created'
EXTRACTION_STARTED = 'extraction_started'
EXTRACTION_SUCCEEDED = 'extraction_succeeded'
EXTRACTION_FAILED = 'extraction_failed'

# Phase 5 - AI eDAR quality validation (docs/phase5-validation-provenance.md
# §Processing events). Emitted inside the extraction stage, before EXTRACTION_SUCCEEDED
# / EXTRACTION_FAILED; no new event subsystem.
QUALITY_VALIDATION_SUCCEEDED = 'quality_validation_succeeded'
QUALITY_VALIDATION_FAILED = 'quality_validation_failed'
