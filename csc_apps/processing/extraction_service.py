"""Runs one EXTRACTION ProcessingJob to completion or failure
(docs/phase4-gemini-edar-extraction.md §Architecture, §Processing job).

Independent from csc_apps.processing.stt_service/translation_service: an extraction
retry consumes the existing English Transcript directly - it never touches Audio,
the original Transcript, SpeechToTextProvider, or TranslationProvider. Not wired to
any TaskRunner automatically and not invoked from GET /recordings/<id>/ (read-only -
docs/phase4-gemini-edar-extraction.md §33); see
csc_apps.processing.management.commands.process_pending_extraction_jobs for how this
gets called in practice.
"""

import logging
import time

from django.db import transaction
from django.utils import timezone

from csc_apps.activity_log.models import ActivityLog
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.schema_loader import load_schema
from csc_apps.edar.schema_validation import validate_extraction_entry
from csc_apps.processing import event_types
from csc_apps.processing.error_classification import is_retryable
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ExtractedField, ExtractionProvider, ProviderError
from csc_apps.processing.providers.gemini.provider import GeminiExtractionProvider
from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys, repeating_field_keys
from csc_apps.recordings.models.audio import Transcript

logger = logging.getLogger(__name__)

_RUNNABLE_STATUSES = ('PENDING', 'RETRYING')


def run_extraction_job(job_id: int, provider: ExtractionProvider | None = None) -> ProcessingJob:
    """Idempotent against re-invocation, same guarantee as
    csc_apps.processing.{stt,translation}_service: a job not currently PENDING/
    RETRYING is returned unchanged."""
    provider = provider or GeminiExtractionProvider()

    job = ProcessingJob.objects.select_related('recording').get(job_id=job_id, job_type='EXTRACTION')
    if job.status not in _RUNNABLE_STATUSES:
        logger.info('extraction_service.skip job_id=%s status=%s', job_id, job.status)
        return job

    recording = job.recording

    job.status = 'RUNNING'
    job.attempt_count += 1
    job.started_at = timezone.now()
    job.save(update_fields=['status', 'attempt_count', 'started_at'])
    ProcessingEvent.objects.create(
        recording=recording, job=job, event_type=event_types.EXTRACTION_STARTED,
        metadata={'attempt': job.attempt_count},
    )
    logger.info(
        'extraction_service.started job_id=%s recording_id=%s attempt=%s',
        job_id, recording.recording_id, job.attempt_count,
    )

    start = time.monotonic()
    english_transcript = Transcript.objects.filter(recording=recording, language='ENGLISH').first()
    if english_transcript is None:
        # Should not happen in practice - this job is only ever created right after
        # translation succeeds (csc_apps.processing.translation_service.
        # _record_success) - but extraction must never fall back to calling
        # translation/STT itself (docs/phase4-gemini-edar-extraction.md §17 in the
        # source instructions), so a missing English transcript is a hard,
        # non-retryable failure here, same pattern as translation_service's missing-
        # original-transcript guard.
        _record_failure(
            job, recording, ProviderError('EXTRACTION_UNSUPPORTED_INPUT', 'No English transcript found'),
            duration_seconds=time.monotonic() - start,
        )
        return job

    schema = load_schema()
    try:
        result = provider.extract(english_text=english_transcript.text, schema=schema)
        rows = _build_field_value_rows(result, schema)
    except ProviderError as e:
        _record_failure(job, recording, e, duration_seconds=time.monotonic() - start)
        return job
    except ValueError as e:
        # A schema/domain-validation failure on otherwise successfully-parsed Gemini
        # output (docs/phase4-gemini-edar-extraction.md §Validation layers) -
        # classified the same as a malformed provider response and not blindly
        # retried (source instructions §30: "do not endlessly retry deterministic
        # schema violations").
        _record_failure(
            job, recording, ProviderError('EXTRACTION_SCHEMA_VALIDATION_FAILED', str(e)),
            duration_seconds=time.monotonic() - start,
        )
        return job

    _record_success(job, recording, result, rows, duration_seconds=time.monotonic() - start)
    return job


def _entry_from_extracted_field(field: ExtractedField) -> dict:
    """Adapts an ExtractedField into the plain-dict shape
    csc_apps.edar.schema_validation.validate_extraction_entry expects
    (docs/ai-extraction-contract.md §2). A field whose evidence text is empty is
    treated as having no source at all - this is what lets validate_extraction_entry
    catch a value Gemini supplied without real supporting evidence (a violation of
    the prompt's own "confidence/evidence must be null iff value is null" rule,
    docs/phase4-gemini-edar-extraction.md §Extraction prompt) as a validation
    failure, rather than silently accepting it."""
    has_source = bool(field.source and field.source.transcript_segment)
    return {
        'field': field.field,
        'value': field.value,
        'confidence': field.confidence,
        'source': {
            'transcript_segment': field.source.transcript_segment,
            'start_time': field.source.start_time,
            'end_time': field.source.end_time,
        } if has_source else None,
    }


def _build_field_value_rows(result, schema: dict) -> list[dict]:
    """Validates every KNOWN field Gemini returned (Layer 1 - structural/type,
    csc_apps.edar.schema_validation.validate_extraction_entry), then reconciles
    against the full expected field-key set for this candidate (the 28 flat fields,
    plus every vehicle/casualty slot Gemini actually considered - result.
    provider_metadata['vehicle_count']/['casualty_count']) so every attempted field
    gets a row: `known=KNOWN` with a value, or `known=UNKNOWN` with none
    (docs/unknown-data-policy.md §2 - a missing row means "not attempted", which
    would misrepresent a field Gemini genuinely considered and found no evidence
    for). Raises ValueError (caught by the caller) on the first validation failure -
    the whole candidate is rejected together, never partially accepted."""
    known_entries = {}
    for field in result.fields:
        entry = _entry_from_extracted_field(field)
        validate_extraction_entry(entry, schema=schema)
        known_entries[entry['field']] = entry

    vehicle_count = result.provider_metadata.get('vehicle_count', 0)
    casualty_count = result.provider_metadata.get('casualty_count', 0)

    expected_keys = list(flat_field_keys(schema))
    vehicle_fields = repeating_field_keys(schema, 'vehicle')
    for index in range(1, vehicle_count + 1):
        expected_keys.extend(f'vehicle.{index}.{base_key}' for base_key in vehicle_fields)
    casualty_fields = repeating_field_keys(schema, 'casualty')
    for index in range(1, casualty_count + 1):
        expected_keys.extend(f'casualty.{index}.{base_key}' for base_key in casualty_fields)

    rows = []
    for field_key in expected_keys:
        entry = known_entries.get(field_key)
        if entry is not None:
            rows.append({
                'field_key': field_key,
                'known': 'KNOWN',
                'value': entry['value'],
                'confidence': entry['confidence'],
                'source_transcript_segment': entry['source']['transcript_segment'],
                'source_start_time': entry['source'].get('start_time'),
                'source_end_time': entry['source'].get('end_time'),
            })
        else:
            rows.append({
                'field_key': field_key,
                'known': 'UNKNOWN',
                'value': None,
                'confidence': None,
                'source_transcript_segment': None,
                'source_start_time': None,
                'source_end_time': None,
            })
    return rows


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
        recording=recording, job=job, event_type=event_types.EXTRACTION_FAILED,
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
        'extraction_service.failed job_id=%s recording_id=%s error_code=%s retryable=%s final_status=%s duration=%.2fs',
        job.job_id, recording.recording_id, error.error_code, retryable, job.status, duration_seconds,
    )


def _record_success(job: ProcessingJob, recording, result, rows: list[dict], duration_seconds: float) -> None:
    with transaction.atomic():
        edar_record, _ = EdarRecord.objects.get_or_create(recording=recording)

        # Idempotent, atomic replace (docs/phase4-gemini-edar-extraction.md
        # §Idempotency, §Transactional persistence, source instructions §36-37): the
        # AI layer's prior rows (if any, from an earlier attempt) are deleted and the
        # new validated set is bulk-created in the same transaction - a retry never
        # leaves stale fields from a previous attempt behind, and a failure partway
        # through this block rolls back the delete too, so there is never a partially
        # persisted candidate.
        EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI').delete()
        EdarFieldValue.objects.bulk_create([
            EdarFieldValue(
                edar_record=edar_record,
                field_key=row['field_key'],
                layer='AI',
                known=row['known'],
                value=row['value'],
                confidence=row['confidence'],
                source_transcript_segment=row['source_transcript_segment'],
                source_start_time=row['source_start_time'],
                source_end_time=row['source_end_time'],
                extraction_version=result.extraction_version,
            )
            for row in rows
        ])

        job.status = 'SUCCEEDED'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])

        known_count = sum(1 for row in rows if row['known'] == 'KNOWN')
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.EXTRACTION_SUCCEEDED,
            metadata={
                'provider': result.provider_name,
                'field_count': len(rows),
                'known_field_count': known_count,
                'vehicle_count': result.provider_metadata.get('vehicle_count'),
                'casualty_count': result.provider_metadata.get('casualty_count'),
                'duration_seconds': round(duration_seconds, 2),
            },
        )
        ActivityLog.record(
            user=recording.officer, action='Create', model='EdarRecord',
            details={'recording_id': recording.recording_id, 'job_id': job.job_id, 'known_field_count': known_count},
        )
    logger.info(
        'extraction_service.succeeded job_id=%s recording_id=%s known_field_count=%s duration=%.2fs',
        job.job_id, recording.recording_id, known_count, duration_seconds,
    )
