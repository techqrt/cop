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
from csc_apps.edar.quality_validation import (
    EVIDENCE_MATCH_RULE,
    STATUS_INVALID,
    STATUS_VALIDATED,
    STATUS_VALIDATION_WARNING,
    assess_candidate,
    compute_metrics,
)
from csc_apps.processing import event_types
from csc_apps.processing.error_classification import is_retryable
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ExtractedField, ExtractionProvider, ProviderError
from csc_apps.processing.providers.gemini.provider import GeminiExtractionProvider
from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys, repeating_field_keys
from csc_apps.recordings.models.audio import Transcript
from csc_apps.recordings.state_machine import can_transition, transition

logger = logging.getLogger(__name__)

_RUNNABLE_STATUSES = ('PENDING', 'RETRYING')


def run_extraction_job(job_id: int, provider: ExtractionProvider | None = None) -> ProcessingJob:
    """Idempotent against re-invocation, same guarantee as
    csc_apps.processing.{stt,translation}_service: a job not currently PENDING/
    RETRYING is returned unchanged."""
    provider = provider or GeminiExtractionProvider()

    job = ProcessingJob.objects.select_related('recording', 'audio').get(job_id=job_id, job_type='EXTRACTION')
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

    # Phase 10B (docs/phase10b-supplemental-audio.md §Job dispatch): a targeted,
    # merge-only extraction for a supplemental audio takes a completely different
    # path from here on - it never deletes or replaces the AI layer, only fills in
    # currently-unknown fields. Dispatched on job.audio.role, not a new job_type -
    # process_pending_extraction_jobs and every other piece of ProcessingJob/
    # ProcessingEvent machinery around this call is unchanged and reused as-is
    # (docs/phase10b-supplemental-audio.md §Reuse, not duplication).
    if job.audio is not None and job.audio.role == 'SUPPLEMENTAL':
        return _run_targeted_extraction(job, recording, provider, start)

    # job.audio, not recording.audio (Phase 10B, docs/phase10b-supplemental-
    # audio.md §Model changes): a Recording can now have more than one Audio row,
    # so which English transcript this job must extract from is only known from
    # the job itself.
    english_transcript = Transcript.objects.filter(audio=job.audio, language='ENGLISH').first()
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


def _run_targeted_extraction(
    job: ProcessingJob, recording, provider: ExtractionProvider, start: float
) -> ProcessingJob:
    """Phase 10B supplemental-audio extraction (docs/phase10b-supplemental-
    audio.md §Targeted extraction, §Merge). Reuses run_extraction_job's English-
    transcript-lookup pattern, the same ExtractionProvider, and
    quality_validation.assess_candidate unchanged - only the schema scope (backend-
    derived eligible fields, never client-supplied) and the persistence step (merge
    into existing AI rows, never delete-then-replace) differ from the path above."""
    audio = job.audio
    english_transcript = Transcript.objects.filter(audio=audio, language='ENGLISH').first()
    if english_transcript is None:
        _record_failure(
            job, recording, ProviderError('EXTRACTION_UNSUPPORTED_INPUT', 'No English transcript found'),
            duration_seconds=time.monotonic() - start,
        )
        return job

    edar_record = EdarRecord.objects.filter(recording=recording).first()
    if edar_record is None:
        _record_failure(
            job, recording,
            ProviderError('EXTRACTION_UNSUPPORTED_INPUT', 'No existing AI eDAR candidate to supplement'),
            duration_seconds=time.monotonic() - start,
        )
        return job

    # The PUT endpoint (csc_apps.recordings.views.RecordingView.supplement_extract)
    # already refuses a new supplemental upload once review_status is APPROVED -
    # but that check runs at upload time, and this job may not run until well
    # after that (it is picked up by process_pending_extraction_jobs on its own
    # schedule, with no coordination with approval). An officer approving the
    # record while this job sits PENDING is a real, reachable race, not a
    # hypothetical one - rechecked here, fresh, and again immediately before the
    # merge commits in _record_targeted_success (docs/phase10b-supplemental-
    # audio.md §Race safety).
    if edar_record.review_status == 'APPROVED':
        _record_failure(
            job, recording,
            ProviderError(
                'EXTRACTION_RECORD_ALREADY_APPROVED',
                'This eDAR record was approved after this supplemental job was queued',
            ),
            duration_seconds=time.monotonic() - start,
        )
        return job

    schema = load_schema()
    ai_rows = list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI'))
    # Recomputed fresh here, not trusted from request time (docs/phase10b-
    # supplemental-audio.md §Race safety) - another supplemental request may have
    # resolved some or all of these fields between this job's creation and now.
    eligible_keys = [r.field_key for r in ai_rows if r.known != 'KNOWN']

    if not eligible_keys:
        # Zero eligible fields left is a valid, non-error outcome, not a failure -
        # the job still succeeds, it just resolves nothing (docs/phase10b-
        # supplemental-audio.md §No-fabrication / zero-resolved outcome).
        _record_targeted_success(
            job, recording, edar_record, resolved_rows=[], requested_field_count=0,
            duration_seconds=time.monotonic() - start,
        )
        return job

    vehicle_count = _max_entity_index(ai_rows, 'vehicle')
    casualty_count = _max_entity_index(ai_rows, 'casualty')
    entity_context_lines = _entity_context_lines(ai_rows)

    try:
        result = provider.extract(
            english_text=english_transcript.text, schema=schema, target_fields=eligible_keys,
            entity_context_lines=entity_context_lines,
        )
    except ProviderError as e:
        _record_failure(job, recording, e, duration_seconds=time.monotonic() - start)
        return job

    # Hard server-side enforcement boundary (docs/phase10b-supplemental-audio.md
    # §Server-side enforcement): even if the provider returns a field outside what
    # was actually asked for, it is discarded here before validation ever sees it -
    # never persisted, never allowed to expand write scope beyond eligible_keys.
    result.fields = [f for f in result.fields if f.field in eligible_keys]

    entries = [_entry_from_extracted_field(f) for f in result.fields]
    assessment = assess_candidate(
        entries=entries, expected_keys=eligible_keys, transcript_text=english_transcript.text,
        schema=schema, vehicle_count=vehicle_count, casualty_count=casualty_count,
    )
    report = assessment.report
    if report['status'] == STATUS_INVALID:
        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.QUALITY_VALIDATION_FAILED,
            metadata={'errorCount': report['metrics']['errorCount'], 'issues': report['errors']},
        )
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

    resolved_rows = [r for r in assessment.rows if r['known'] == 'KNOWN']
    _record_targeted_success(
        job, recording, edar_record, resolved_rows=resolved_rows, requested_field_count=len(eligible_keys),
        duration_seconds=time.monotonic() - start, extraction_version=result.extraction_version,
        targeted_warnings=report['warnings'],
    )
    return job


def _max_entity_index(ai_rows: list, entity: str) -> int:
    """Highest vehicle/casualty index with at least one AI row - the entity-count
    context a targeted call's quality_validation._check_entity_limit needs, derived
    from what the original extraction already established rather than re-declared
    by anyone (docs/phase10b-supplemental-audio.md §Entity matching)."""
    indices = {int(row.field_key.split('.')[1]) for row in ai_rows if row.field_key.startswith(f'{entity}.')}
    return max(indices) if indices else 0


def _entity_context_lines(ai_rows: list) -> list[str]:
    """Human-readable "vehicle 1: ..." / "casualty 2: ..." lines built from every
    currently-KNOWN AI field of a repeating entity - passed to the targeted prompt
    so Gemini can match new information in the supplemental audio to the right
    existing index (docs/phase10b-supplemental-audio.md §Entity matching). Never
    includes a flat (non-repeating) field or a still-UNKNOWN one (nothing to
    summarize)."""
    by_entity_index: dict[tuple[str, int], dict[str, object]] = {}
    for row in ai_rows:
        if row.known != 'KNOWN':
            continue
        parts = row.field_key.split('.')
        if len(parts) != 3:
            continue
        entity, index_str, base_key = parts
        by_entity_index.setdefault((entity, int(index_str)), {})[base_key] = row.value

    lines = []
    for (entity, index), values in sorted(by_entity_index.items()):
        summary = ', '.join(f'{k}={v}' for k, v in sorted(values.items()))
        lines.append(f'{entity} {index}: {summary}')
    return lines


def _record_targeted_success(
    job: ProcessingJob, recording, edar_record, resolved_rows: list[dict], requested_field_count: int,
    duration_seconds: float, extraction_version: str | None = None, targeted_warnings: list[dict] | None = None,
) -> None:
    with transaction.atomic():
        # Second, narrower half of the approval race guard (see the caller's own
        # check): locks the EdarRecord row and re-reads review_status immediately
        # before merging anything, closing the window between the caller's check
        # and this commit (e.g. the time spent inside the provider.extract() call
        # above). Under Postgres this also serializes against a concurrent
        # approve_edar() write to the same row; under SQLite select_for_update()
        # is a no-op (Django's own documented behavior), so this narrower half is
        # correctness-under-Postgres only - the caller's earlier check is what
        # covers the realistic case regardless of database engine.
        locked_edar_record = EdarRecord.objects.select_for_update().get(pk=edar_record.pk)
        if locked_edar_record.review_status == 'APPROVED':
            _record_failure(
                job, recording,
                ProviderError(
                    'EXTRACTION_RECORD_ALREADY_APPROVED',
                    'This eDAR record was approved while this supplemental job was running',
                ),
                duration_seconds=duration_seconds,
            )
            return

        resolved_keys: list[str] = []
        if resolved_rows:
            # Merge only - never a delete-then-replace of the AI layer (docs/
            # phase10b-supplemental-audio.md §Already-KNOWN protection): every
            # already-KNOWN AI field, and every field outside this call's eligible
            # set entirely, is left byte-unchanged. select_for_update plus a fresh
            # known != 'KNOWN' recheck protects against a race with a concurrent
            # supplemental request that targeted an overlapping field and committed
            # first - this merge then only applies to whatever is still unresolved
            # at commit time.
            rows_by_key = {
                v.field_key: v for v in EdarFieldValue.objects.select_for_update().filter(
                    edar_record=edar_record, layer='AI', field_key__in=[r['field_key'] for r in resolved_rows],
                )
            }
            to_update = []
            for row in resolved_rows:
                field_value = rows_by_key.get(row['field_key'])
                if field_value is None or field_value.known == 'KNOWN':
                    continue
                field_value.known = row['known']
                field_value.value = row['value']
                field_value.confidence = row['confidence']
                field_value.source_transcript_segment = row['source_transcript_segment']
                field_value.source_start_time = row['source_start_time']
                field_value.source_end_time = row['source_end_time']
                field_value.extraction_version = extraction_version
                to_update.append(field_value)
            if to_update:
                EdarFieldValue.objects.bulk_update(
                    to_update,
                    ['known', 'value', 'confidence', 'source_transcript_segment', 'source_start_time',
                     'source_end_time', 'extraction_version'],
                )
            resolved_keys = [fv.field_key for fv in to_update]
            _refresh_quality_summary(edar_record, resolved_keys, targeted_warnings or [])

        job.status = 'SUCCEEDED'
        job.completed_at = timezone.now()
        job.provider_metadata = {}
        job.save(update_fields=['status', 'completed_at', 'provider_metadata'])

        ProcessingEvent.objects.create(
            recording=recording, job=job, event_type=event_types.EXTRACTION_SUCCEEDED,
            metadata={
                'mode': 'supplemental',
                'audio_id': job.audio_id,
                'requested_field_count': requested_field_count,
                'resolved_field_count': len(resolved_keys),
                'resolved_field_keys': resolved_keys,
                'duration_seconds': round(duration_seconds, 2),
            },
        )
        ActivityLog.record(
            user=recording.officer, action='Update', model='EdarRecord',
            details={
                'recording_id': recording.recording_id, 'job_id': job.job_id, 'audio_id': job.audio_id,
                'event': 'supplemental_extraction', 'requested_field_count': requested_field_count,
                'resolved_field_keys': resolved_keys,
            },
        )
    logger.info(
        'extraction_service.supplemental_succeeded job_id=%s recording_id=%s requested=%s resolved=%s duration=%.2fs',
        job.job_id, recording.recording_id, requested_field_count, len(resolved_keys), duration_seconds,
    )


def _refresh_quality_summary(edar_record, resolved_keys: list[str], new_warnings: list[dict]) -> None:
    """Recomputes EdarRecord.quality_status/quality_report after a supplemental
    merge (docs/phase10b-supplemental-audio.md §Quality summary refresh) - without
    re-running assess_candidate over the whole candidate, which would require a
    single transcript_text and so would wrongly re-check the original fields'
    evidence against the supplemental transcript. Warnings for the fields this
    merge touched are replaced with the targeted assessment's own warnings for
    those exact fields; every other field's existing warnings are kept unchanged
    verbatim. There are never merge-time errors here - only a validated
    (non-INVALID) assessment ever reaches this function."""
    old_report = edar_record.quality_report or {}
    kept_warnings = [w for w in old_report.get('warnings', []) if w.get('field') not in resolved_keys]
    warnings = kept_warnings + new_warnings

    schema = load_schema()
    rows = list(
        EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI').values(
            'field_key', 'known', 'confidence', 'source_transcript_segment'
        )
    )
    metrics = compute_metrics(rows, schema, warnings, errors=[])
    status = STATUS_VALIDATION_WARNING if warnings else STATUS_VALIDATED

    edar_record.quality_status = status
    edar_record.quality_report = {
        'status': status,
        'errors': [],
        'warnings': warnings,
        'metrics': metrics,
        'evidenceMatchRule': old_report.get('evidenceMatchRule', EVIDENCE_MATCH_RULE),
    }
    edar_record.save(update_fields=['quality_status', 'quality_report'])


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

        # docs/processing-pipeline.md §Persist AI Result, docs/recording-state-
        # machine.md §READY_FOR_REVIEW: an AI eDAR candidate now exists, which is
        # exactly what that state represents - deferred through Phase 2-5 pending an
        # actual review mechanism (Phase 6). Guarded by can_transition rather than
        # asserting PROCESSING, so re-extracting an already-reviewed recording never
        # raises here - review/approval state is untouched either way.
        if can_transition(recording.status, 'READY_FOR_REVIEW'):
            transition(recording, 'READY_FOR_REVIEW')

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
