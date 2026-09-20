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
from csc_apps.edar.quality_validation import STATUS_INVALID, assess_candidate
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
    except ProviderError as e:
        _record_failure(job, recording, e, duration_seconds=time.monotonic() - start)
        return job

    # Authoritative validation happens here, before anything is persisted or any prior
    # AI candidate is touched (docs/phase5-validation-provenance.md §Persistence): a
    # candidate that fails deterministic validation never reaches the delete-then-
    # create below, so there is no data-loss window for an earlier valid candidate.
    assessment = _assess(result, schema, english_transcript.text)
    report = assessment.report
    if report['status'] == STATUS_INVALID:
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.QUALITY_VALIDATION_FAILED,
            metadata={'errorCount': report['metrics']['errorCount'], 'issues': report['errors']},
        )
        # Non-retryable: a deterministic schema/quality violation is not expected to
        # change on a retry (source instructions §19/§30). The generic message never
        # includes field values or transcript text.
        _record_failure(
            job, recording,
            ProviderError('EXTRACTION_SCHEMA_VALIDATION_FAILED', f'{len(report["errors"])} validation error(s)'),
            duration_seconds=time.monotonic() - start, report=report,
        )
        return job

    ProcessingEvent.objects.create(
        recording=recording, job=job, event_type=event_types.QUALITY_VALIDATION_SUCCEEDED,
        metadata={
            'status': report['status'], 'warningCount': report['metrics']['warningCount'],
            'knownFields': report['metrics']['knownFields'],
        },
    )
    _record_success(
        job, recording, english_transcript, result, assessment, duration_seconds=time.monotonic() - start
    )
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


def _assess(result, schema: dict, transcript_text: str):
    """Builds the entry dicts and the full expected field-key set (the 28 flat fields
    plus every vehicle/casualty slot Gemini's `vehicle_count`/`casualty_count`
    metadata says it considered - docs/unknown-data-policy.md §2: an attempted field
    with no evidence is `UNKNOWN`, not a missing row), then hands both to the
    deterministic quality validator (csc_apps.edar.quality_validation)."""
    entries = [_entry_from_extracted_field(f) for f in result.fields]
    vehicle_count = result.provider_metadata.get('vehicle_count', 0)
    casualty_count = result.provider_metadata.get('casualty_count', 0)

    expected_keys = list(flat_field_keys(schema))
    # Slots beyond the schema's cap are never turned into rows - assess_candidate
    # reports that as an entity_limit_exceeded error instead.
    vehicle_slots = min(vehicle_count, _module_max(schema, 'vehicle'))
    vehicle_fields = repeating_field_keys(schema, 'vehicle')
    for index in range(1, vehicle_slots + 1):
        expected_keys.extend(f'vehicle.{index}.{base_key}' for base_key in vehicle_fields)
    casualty_fields = repeating_field_keys(schema, 'casualty')
    for index in range(1, casualty_count + 1):
        expected_keys.extend(f'casualty.{index}.{base_key}' for base_key in casualty_fields)

    return assess_candidate(
        entries=entries, expected_keys=expected_keys, transcript_text=transcript_text, schema=schema,
        vehicle_count=vehicle_count, casualty_count=casualty_count,
    )


def _module_max(schema: dict, entity: str) -> int:
    module = next(m for m in schema['modules'] if m.get('repeat_entity') == entity)
    return module.get('max_repetitions') or 0


def _record_failure(
    job: ProcessingJob, recording, error: ProviderError, duration_seconds: float, report: dict | None = None
) -> None:
    retryable = is_retryable(error.error_code)
    exhausted = job.attempt_count >= job.max_attempts
    job.status = 'FAILED' if (not retryable or exhausted) else 'RETRYING'
    job.is_retryable = retryable
    job.error_code = error.error_code
    job.error_message = str(error)
    update_fields = ['status', 'is_retryable', 'error_code', 'error_message', 'completed_at']
    if report is not None:
        # The structured, value-free validation report (field / code / severity /
        # message per issue) - what the API surfaces as `extractionIssues`. Stored on
        # the failed job rather than as eDAR rows: an INVALID candidate is never
        # persisted as AI data (docs/phase5-validation-provenance.md §Persistence).
        job.provider_metadata = {'validation_report': report}
        update_fields.append('provider_metadata')
    if job.status == 'FAILED':
        job.completed_at = timezone.now()
    job.save(update_fields=update_fields)

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
        'extraction_service.failed job_id=%s recording_id=%s error_code=%s retryable=%s final_status=%s '
        'validation_errors=%s duration=%.2fs',
        job.job_id, recording.recording_id, error.error_code, retryable, job.status,
        report['metrics']['errorCount'] if report else 0, duration_seconds,
    )


def _record_success(
    job: ProcessingJob, recording, english_transcript, result, assessment, duration_seconds: float
) -> None:
    rows, report = assessment.rows, assessment.report
    with transaction.atomic():
        edar_record, _ = EdarRecord.objects.get_or_create(recording=recording)

        # Idempotent, atomic replace (docs/phase4-gemini-edar-extraction.md
        # §Idempotency): the AI layer's prior rows are deleted and the new validated
        # set bulk-created in one transaction, together with the record-level
        # provenance/quality below - a failure anywhere in this block rolls all of it
        # back, so status, transcript/job linkage and field rows can never disagree.
        # The delete only runs here, after validation already passed, so a candidate
        # that fails validation never removes an earlier valid one.
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

        # Reprocessing replaces these with the new run's identity - the current AI
        # candidate always points at the transcript/job/version that produced it.
        # Historical candidates are not retained (documented limitation).
        edar_record.quality_status = report['status']
        edar_record.quality_report = report
        edar_record.source_transcript = english_transcript
        edar_record.extraction_job = job
        edar_record.extracted_at = timezone.now()
        edar_record.save(
            update_fields=['quality_status', 'quality_report', 'source_transcript', 'extraction_job', 'extracted_at']
        )

        job.status = 'SUCCEEDED'
        job.completed_at = timezone.now()
        job.provider_metadata = {}
        job.save(update_fields=['status', 'completed_at', 'provider_metadata'])

        metrics = report['metrics']
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.EXTRACTION_SUCCEEDED,
            metadata={
                'provider': result.provider_name,
                'field_count': metrics['totalFields'],
                'known_field_count': metrics['knownFields'],
                'quality_status': report['status'],
                'warning_count': metrics['warningCount'],
                'vehicle_count': result.provider_metadata.get('vehicle_count'),
                'casualty_count': result.provider_metadata.get('casualty_count'),
                'duration_seconds': round(duration_seconds, 2),
            },
        )
        ActivityLog.record(
            user=recording.officer, action='Create', model='EdarRecord',
            details={
                'recording_id': recording.recording_id, 'job_id': job.job_id,
                'known_field_count': metrics['knownFields'], 'quality_status': report['status'],
            },
        )
    logger.info(
        'extraction_service.succeeded job_id=%s recording_id=%s extraction_version=%s quality_status=%s '
        'fields=%s known=%s warnings=%s duration=%.2fs',
        job.job_id, recording.recording_id, result.extraction_version, report['status'],
        metrics['totalFields'], metrics['knownFields'], metrics['warningCount'], duration_seconds,
    )
