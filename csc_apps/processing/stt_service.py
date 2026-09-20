"""Runs one STT ProcessingJob to completion or failure
(docs/phase2-sarvam-stt.md §Architecture, §Processing job).

Not wired to any TaskRunner automatically and not invoked from the Phase 1 upload
request - see csc_apps.processing.management.commands.process_pending_stt_jobs for
how this gets called in practice (docs/phase2-sarvam-stt.md §Task execution).
"""

import logging
import os
import shutil
import tempfile
import time

from django.db import transaction
from django.utils import timezone

from csc_apps.activity_log.models import ActivityLog
from csc_apps.processing import event_types
from csc_apps.processing.error_classification import is_retryable
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ProviderError, SpeechToTextProvider
from csc_apps.processing.providers.sarvam.provider import SarvamSpeechToTextProvider
from csc_apps.recordings.models.audio import Transcript
from csc_apps.recordings.storage import get_storage
from csc_apps.recordings.validators import AUDIO_ALLOWED_CONTENT_TYPES

logger = logging.getLogger(__name__)

_RUNNABLE_STATUSES = ('PENDING', 'RETRYING')


def run_stt_job(job_id: int, provider: SpeechToTextProvider | None = None) -> ProcessingJob:
    """Idempotent against re-invocation: a job not currently PENDING/RETRYING (i.e.
    already RUNNING, SUCCEEDED, or terminally FAILED) is returned unchanged
    (docs/phase2-sarvam-stt.md §Idempotency) - repeated command invocations never
    create a second canonical transcript for an already-succeeded job.
    """
    provider = provider or SarvamSpeechToTextProvider()

    job = ProcessingJob.objects.select_related('recording').get(job_id=job_id, job_type='STT')
    if job.status not in _RUNNABLE_STATUSES:
        logger.info('stt_service.skip job_id=%s status=%s', job_id, job.status)
        return job

    recording = job.recording
    audio = recording.audio

    job.status = 'RUNNING'
    job.attempt_count += 1
    job.started_at = timezone.now()
    job.save(update_fields=['status', 'attempt_count', 'started_at'])
    ProcessingEvent.objects.create(
        recording=recording, job=job, event_type=event_types.STT_STARTED,
        metadata={'attempt': job.attempt_count},
    )
    logger.info(
        'stt_service.started job_id=%s recording_id=%s attempt=%s', job_id, recording.recording_id, job.attempt_count
    )

    start = time.monotonic()
    try:
        result = _transcribe_via_temp_copy(provider, audio)
    except ProviderError as e:
        _record_failure(job, recording, e, duration_seconds=time.monotonic() - start)
        return job

    _record_success(job, recording, result, duration_seconds=time.monotonic() - start)
    return job


def _transcribe_via_temp_copy(provider: SpeechToTextProvider, audio):
    """Retrieves the immutable source audio through AudioStorage, writes a temporary
    local copy for the provider to read, and guarantees that copy is deleted
    afterwards - the canonical stored file is never touched, modified, or exposed
    (docs/phase2-sarvam-stt.md §11 Audio retrieval, §12 Immutable source audio)."""
    storage = get_storage()
    extension = AUDIO_ALLOWED_CONTENT_TYPES.get(audio.content_type, '')
    with tempfile.TemporaryDirectory(prefix='csc-stt-') as tmp_dir:
        local_path = os.path.join(tmp_dir, f'audio{extension}')
        with storage.open(audio.storage_path) as src, open(local_path, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        return provider.transcribe(local_audio_path=local_path, content_type=audio.content_type)


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
        recording=recording, job=job, event_type=event_types.STT_FAILED,
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
        'stt_service.failed job_id=%s recording_id=%s error_code=%s retryable=%s final_status=%s duration=%.2fs',
        job.job_id, recording.recording_id, error.error_code, retryable, job.status, duration_seconds,
    )


def _record_success(job: ProcessingJob, recording, result, duration_seconds: float) -> None:
    with transaction.atomic():
        # unique_together on (recording, language) - see docs/domain-model.md - makes
        # this the retry-safety/idempotency guarantee: a second successful run for the
        # same recording updates the one ORIGINAL-language row rather than duplicating
        # it (docs/phase2-sarvam-stt.md §Retry safety, §Idempotency).
        Transcript.objects.update_or_create(
            recording=recording,
            language='ORIGINAL',
            defaults={
                'text': result.text,
                'detected_language_code': result.detected_language_code,
                'provider_name': result.provider_name,
                'provider_metadata': result.provider_metadata,
            },
        )
        job.status = 'SUCCEEDED'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.STT_SUCCEEDED,
            metadata={'provider': result.provider_name, 'duration_seconds': round(duration_seconds, 2)},
        )
        ActivityLog.record(
            user=recording.officer, action='Create', model='Transcript',
            details={'recording_id': recording.recording_id, 'job_id': job.job_id},
        )

        # Phase 3 (docs/phase3-sarvam-translation.md §Architecture): STT succeeding is
        # what makes a recording eligible for translation - chain the next stage's
        # job the same way Phase 1's upload created this STT job. get_or_create
        # guards against ever creating a second TRANSLATION job for one recording,
        # even if _record_success were somehow invoked more than once (it isn't, in
        # practice - run_stt_job's PENDING/RETRYING guard prevents that - but this
        # keeps the guarantee true by construction, not just by the caller's care).
        translation_job, created = ProcessingJob.objects.get_or_create(
            recording=recording, job_type='TRANSLATION', defaults={'status': 'PENDING'}
        )
        if created:
            ProcessingEvent.objects.create(
                recording=recording, job=translation_job, event_type=event_types.TRANSLATION_JOB_CREATED,
                metadata={'job_type': translation_job.job_type},
            )
    logger.info(
        'stt_service.succeeded job_id=%s recording_id=%s duration=%.2fs',
        job.job_id, recording.recording_id, duration_seconds,
    )
