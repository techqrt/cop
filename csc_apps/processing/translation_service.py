"""Runs one TRANSLATION ProcessingJob to completion or failure
(docs/phase3-sarvam-translation.md §Architecture, §Processing job).

Independent from csc_apps.processing.stt_service: a translation retry consumes the
existing original Transcript directly - it never touches Audio, AudioStorage, or the
SpeechToTextProvider (docs/phase3-sarvam-translation.md §Translation job lifecycle).
Not wired to any TaskRunner automatically and not invoked from GET /recordings/<id>/
(read-only - docs/phase3-sarvam-translation.md §No provider call from GET); see
csc_apps.processing.management.commands.process_pending_translation_jobs for how
this gets called in practice.
"""

import logging
import time

from django.db import transaction
from django.utils import timezone

from csc_apps.activity_log.models import ActivityLog
from csc_apps.processing import event_types
from csc_apps.processing.error_classification import is_retryable
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ProviderError, TranslationProvider
from csc_apps.processing.providers.sarvam.translation_provider import SarvamTranslationProvider
from csc_apps.recordings.models.audio import Transcript

logger = logging.getLogger(__name__)

_RUNNABLE_STATUSES = ('PENDING', 'RETRYING')
TARGET_LANGUAGE_CODE = 'en-IN'


def run_translation_job(job_id: int, provider: TranslationProvider | None = None) -> ProcessingJob:
    """Idempotent against re-invocation, same guarantee as
    csc_apps.processing.stt_service.run_stt_job: a job not currently PENDING/RETRYING
    is returned unchanged, so repeated command invocations never create a second
    canonical English transcript for an already-succeeded job."""
    provider = provider or SarvamTranslationProvider()

    job = ProcessingJob.objects.select_related('recording').get(job_id=job_id, job_type='TRANSLATION')
    if job.status not in _RUNNABLE_STATUSES:
        logger.info('translation_service.skip job_id=%s status=%s', job_id, job.status)
        return job

    recording = job.recording

    job.status = 'RUNNING'
    job.attempt_count += 1
    job.started_at = timezone.now()
    job.save(update_fields=['status', 'attempt_count', 'started_at'])
    ProcessingEvent.objects.create(
        recording=recording, job=job, event_type=event_types.TRANSLATION_STARTED,
        metadata={'attempt': job.attempt_count},
    )
    logger.info(
        'translation_service.started job_id=%s recording_id=%s attempt=%s',
        job_id, recording.recording_id, job.attempt_count,
    )

    start = time.monotonic()
    original_transcript = Transcript.objects.filter(recording=recording, language='ORIGINAL').first()
    if original_transcript is None:
        # Should not happen in practice - this job is only ever created right after
        # STT succeeds (csc_apps.processing.stt_service._record_success) - but a
        # translation job must never fall back to calling STT itself to produce one
        # (docs/phase3-sarvam-translation.md §Translation job lifecycle), so a
        # missing original transcript is a hard, non-retryable failure here.
        _record_failure(
            job, recording, ProviderError('TRANSLATION_UNSUPPORTED_INPUT', 'No original transcript found'),
            duration_seconds=time.monotonic() - start,
        )
        return job

    try:
        result = provider.translate(
            text=original_transcript.text,
            source_language_code=original_transcript.detected_language_code,
            target_language_code=TARGET_LANGUAGE_CODE,
        )
    except ProviderError as e:
        _record_failure(job, recording, e, duration_seconds=time.monotonic() - start)
        return job

    _record_success(job, recording, result, duration_seconds=time.monotonic() - start)
    return job


def _record_failure(job: ProcessingJob, recording, error: ProviderError, duration_seconds: float) -> None:
    retryable = is_retryable(error.error_code)
    exhausted = job.attempt_count >= job.max_attempts
    job.status = 'FAILED' if (not retryable or exhausted) else 'RETRYING'
    job.is_retryable = retryable
    job.error_code = error.error_code
    job.error_message = str(error)
    if job.status == 'FAILED':
        job.completed_at = timezone.now()
    job.save(update_fields=['status', 'is_retryable', 'error_code', 'error_message', 'completed_at'])

    ProcessingEvent.objects.create(
        recording=recording, job=job, event_type=event_types.TRANSLATION_FAILED,
        metadata={
            'error_code': error.error_code,
            'is_retryable': retryable,
            'attempt': job.attempt_count,
            'max_attempts': job.max_attempts,
            'final_status': job.status,
            'duration_seconds': round(duration_seconds, 2),
        },
    )
    logger.warning(
        'translation_service.failed job_id=%s recording_id=%s error_code=%s retryable=%s final_status=%s duration=%.2fs',
        job.job_id, recording.recording_id, error.error_code, retryable, job.status, duration_seconds,
    )


def _record_success(job: ProcessingJob, recording, result, duration_seconds: float) -> None:
    with transaction.atomic():
        # unique_together on (recording, language) (docs/domain-model.md) - the same
        # retry-safety guarantee as the original transcript: a second successful run
        # updates the one ENGLISH-language row rather than duplicating it
        # (docs/phase3-sarvam-translation.md §Idempotency).
        Transcript.objects.update_or_create(
            recording=recording,
            language='ENGLISH',
            defaults={
                'text': result.text,
                # detected_language_code holds the language *this row's text* is
                # written in - target_language_code (en-IN), matching the ORIGINAL
                # row's same-field semantics ("the language of Transcript.text") and
                # the API contract's `transcript.english.language` example
                # (docs/phase3-sarvam-translation.md §API response). The source
                # language this was translated *from* is provenance, not identity -
                # kept in provider_metadata instead, right below.
                'detected_language_code': result.target_language_code,
                'provider_name': result.provider_name,
                'provider_metadata': {**result.provider_metadata, 'source_language_code': result.source_language_code},
            },
        )
        job.status = 'SUCCEEDED'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.TRANSLATION_SUCCEEDED,
            metadata={
                'provider': result.provider_name,
                'chunk_count': result.provider_metadata.get('chunk_count'),
                'duration_seconds': round(duration_seconds, 2),
            },
        )
        ActivityLog.record(
            user=recording.officer, action='Create', model='Transcript',
            details={'recording_id': recording.recording_id, 'job_id': job.job_id, 'language': 'ENGLISH'},
        )

        # Phase 4 (docs/phase4-gemini-edar-extraction.md §32 Automatic chaining):
        # translation succeeding is what makes a recording eligible for eDAR
        # extraction - chain the next stage's job the same way STT succeeding
        # chained this one (csc_apps.processing.stt_service._record_success).
        # get_or_create guards against ever creating a second EXTRACTION job for one
        # recording, same reasoning as the STT->TRANSLATION chain.
        extraction_job, created = ProcessingJob.objects.get_or_create(
            recording=recording, job_type='EXTRACTION', defaults={'status': 'PENDING'}
        )
        if created:
            ProcessingEvent.objects.create(
                recording=recording, job=extraction_job, event_type=event_types.EXTRACTION_JOB_CREATED,
                metadata={'job_type': extraction_job.job_type},
            )
    logger.info(
        'translation_service.succeeded job_id=%s recording_id=%s duration=%.2fs',
        job.job_id, recording.recording_id, duration_seconds,
    )
