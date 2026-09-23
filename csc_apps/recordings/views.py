import datetime as dt

from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.common.common import Common
from csc_apps.common.utils import Utils
from csc_apps.edar import approval_service, export_service
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing import event_types
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.recordings.dataclasses.request.approve_edar import ApproveEdarRequest
from csc_apps.recordings.dataclasses.request.export_edar import ExportEdarRequest
from csc_apps.recordings.dataclasses.request.get_all_recordings import GetAllRecordingsRequest
from csc_apps.recordings.dataclasses.request.get_recording import GetRecordingRequest
from csc_apps.recordings.dataclasses.request.list_recordings import ListRecordingsRequest
from csc_apps.recordings.dataclasses.request.upload_recording import UploadRecordingRequest
from csc_apps.recordings.models.audio import Audio, Transcript
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.serializers.response.export_edar import RecordingExportResponseSerializer
from csc_apps.recordings.serializers.response.get_all_recordings import RecordingGetAllResponseSerializer
from csc_apps.recordings.serializers.response.get_recording import RecordingDetailResponseSerializer
from csc_apps.recordings.serializers.response.list_recordings import RecordingListResponseSerializer
from csc_apps.recordings.serializers.response.upload_recording import RecordingUploadResponseSerializer
from csc_apps.recordings.state_machine import transition
from csc_apps.recordings.storage import get_storage
from csc_apps.recordings.validators import validate_audio_upload

# A duplicate-submission signal only looks back this far - long enough to catch an
# obvious client-side retry/double-submit, short enough that a genuinely new
# recording that happens to reuse old evidence doesn't get flagged. Safe-minimum
# idempotency signal, not a distributed idempotency-key mechanism
# (docs/open-decisions.md OD-011).
_DUPLICATE_DETECTION_WINDOW = dt.timedelta(minutes=5)


class RecordingView:
    def __init__(self):
        self.data_upload = 'Audio uploaded and queued for processing'
        self.recording_not_found = 'Recording not found'
        self.not_allowed = 'Not allowed to access this recording'
        self.data_list = 'Recordings retrieved successfully'
        self.page_limit_exceeded = 'Page limit exceed!'
        self.data_export = 'Approved eDAR export retrieved successfully'
        self.data_get_all = 'Recordings retrieved successfully.'

    @Common(response_handler=RecordingUploadResponseSerializer).exception_handler
    def upload_extract(self, params: UploadRecordingRequest) -> Response:
        # Validated before any database write, so a rejected upload never creates an
        # orphan Recording (docs/phase1-audio-ingestion.md §Validation).
        validated = validate_audio_upload(params.audio)
        officer = User.objects.get(user_id=params.user_id)

        with transaction.atomic():
            recording = Recording.objects.create(
                officer=officer,
                status='CREATED',
                gps_latitude=params.gps_latitude,
                gps_longitude=params.gps_longitude,
                gps_captured_at=(
                    timezone.now()
                    if params.gps_latitude is not None and params.gps_longitude is not None
                    else None
                ),
                road_name=params.road_name,
                police_station_jurisdiction=params.police_station_jurisdiction,
                case_fir_number=params.case_fir_number,
            )
            ProcessingEvent.objects.create(
                recording=recording,
                event_type=event_types.RECORDING_CREATED,
                metadata={'officer_id': officer.user_id},
            )
            ProcessingEvent.objects.create(
                recording=recording,
                event_type=event_types.AUDIO_VALIDATED,
                metadata={'content_type': params.audio.content_type, 'declared_size_bytes': params.audio.size},
            )

            storage = get_storage()
            # storage.save() is not part of this DB transaction - a database failure
            # cannot roll it back. Everything from here on is wrapped so that if any
            # later step in this block fails, the just-written file is best-effort
            # deleted before the exception propagates (documented boundary:
            # docs/phase1-audio-ingestion.md §Transaction and storage-failure handling
            # - this delete can itself fail, e.g. the same disk fault that caused the
            # original failure, in which case an orphaned file is the accepted
            # residual risk of a non-transactional storage backend).
            stored = storage.save(
                recording_id=recording.recording_id, extension=validated.extension, fileobj=params.audio
            )
            try:
                audio = Audio.objects.create(
                    recording=recording,
                    source='UPLOAD',
                    storage_path=stored.storage_path,
                    content_type=params.audio.content_type,
                    original_filename=validated.original_filename,
                    file_size_bytes=stored.size_bytes,
                    checksum_sha256=stored.checksum_sha256,
                )
                ProcessingEvent.objects.create(
                    recording=recording,
                    event_type=event_types.AUDIO_STORED,
                    metadata={'audio_id': audio.audio_id, 'size_bytes': stored.size_bytes},
                )

                transition(recording, 'UPLOADED')

                # The future AI pipeline's unit of work - left PENDING. Phase 1 does
                # not enqueue it on a TaskRunner: there is no provider yet to run
                # (docs/processing-pipeline.md §3, Sarvam integration is Phase 2), so
                # there is nothing meaningful to execute. Creating this row is the
                # "scheduling" Phase 1 is responsible for.
                job = ProcessingJob.objects.create(recording=recording, job_type='STT', status='PENDING')
                ProcessingEvent.objects.create(
                    recording=recording,
                    job=job,
                    event_type=event_types.PROCESSING_JOB_CREATED,
                    metadata={'job_type': job.job_type},
                )

                # A PENDING job for this recording exists now, which is exactly
                # docs/recording-state-machine.md's definition of PROCESSING ("at
                # least one ProcessingJob... running or pending") - not a claim that
                # processing has actually started.
                transition(recording, 'PROCESSING')

                ActivityLog.record(
                    user=officer,
                    action='Create',
                    model='Recording',
                    details={'recording_id': recording.recording_id, 'audio_id': audio.audio_id},
                )

                duplicate_of_recording_id = self._find_recent_duplicate(
                    officer=officer, checksum=stored.checksum_sha256, exclude_recording_id=recording.recording_id
                )
            except Exception:
                storage.delete(stored.storage_path)
                raise

        return Response(
            status=status.HTTP_201_CREATED,
            data=Utils.success_response_data(
                message=self.data_upload,
                data={
                    'recordingId': recording.recording_id,
                    'status': recording.status,
                    'audioId': audio.audio_id,
                    'processingJobId': job.job_id,
                    'possibleDuplicateOfRecordingId': duplicate_of_recording_id,
                },
            ),
        )

    @Common(response_handler=RecordingListResponseSerializer).exception_handler
    def list_extract(self, params: ListRecordingsRequest) -> Response:
        """History/search (docs/phase7-history-search.md). Read-only, same as GET
        /recordings/<id>/ - never starts/retries processing, never mutates a row.

        Authorization is applied at the database query level, before pagination or
        any filter (docs/phase7-history-search.md §Authorization, §No IDOR): every
        role - officer, REVIEWER, ADMIN alike - is scoped to recordings they
        personally own. Deliberately narrower than GET/<id>/'s owner-OR-REVIEWER/
        ADMIN rule; a REVIEWER/ADMIN can still open any recording directly if they
        already have the ID (unchanged), but will not see it in their own list."""
        requesting_user = User.objects.get(user_id=params.user_id)
        queryset = Recording.objects.filter(officer=requesting_user)

        if params.status:
            queryset = queryset.filter(status=params.status)
        if params.review_status:
            queryset = queryset.filter(edar_record__review_status=params.review_status)
        if params.road_name:
            queryset = queryset.filter(road_name__icontains=params.road_name)
        if params.case_fir_number:
            queryset = queryset.filter(case_fir_number=params.case_fir_number)
        if params.created_from:
            queryset = queryset.filter(created_at__date__gte=params.created_from)
        if params.created_to:
            queryset = queryset.filter(created_at__date__lte=params.created_to)

        # Deterministic default ordering (source instructions §22) - newest first,
        # recording_id as a tiebreaker so two recordings created in the same
        # instant still sort deterministically. No client-supplied sort field -
        # not required beyond deterministic ordering, so not added.
        queryset = queryset.order_by('-created_at', '-recording_id')

        pages = Paginator(queryset, per_page=params.limit)
        if params.page_num > pages.num_pages:
            raise ValueError(self.page_limit_exceeded)
        page = pages.page(params.page_num)

        data = Utils.add_page_parameter(
            final_data=self._build_list_items(list(page)),
            page_num=params.page_num,
            total_page=pages.num_pages,
            present_url=params.present_url,
            next_page_required=params.page_num != pages.num_pages,
        )
        return Response(status=status.HTTP_200_OK, data=Utils.success_response_data(message=self.data_list, data=data))

    @Common(response_handler=RecordingGetAllResponseSerializer).exception_handler
    def get_all_extract(self, params: GetAllRecordingsRequest) -> Response:
        """GET /recordings/get_all/ (docs/phase10a-get-all-and-smoke-test.md) - a
        lightweight index of every recording accessible to the authenticated user,
        deliberately distinct from the paginated, filterable GET /recordings/
        (Phase 7). No pagination, no filters, no per-stage status breakdown - just
        enough to identify a recording and know its current lifecycle state.

        Authorization: identical scoping to GET /recordings/ (docs/phase9-security-
        audit-observability.md §4/§9 - every role, including REVIEWER/ADMIN, sees
        only recordings they personally created in a *list* context; the existing
        GET /recordings/<id>/ remains the endpoint where REVIEWER/ADMIN can reach
        any recording by ID). Applied at the query level, not filtered in Python.

        `status` is `Recording.status` verbatim - the one canonical lifecycle
        field this system already has (docs/recording-state-machine.md); not a
        second, competing status derived from ProcessingJob/ProcessingEvent.

        One query total: `.values(...)` projects only the three columns this
        response needs, so there is no per-recording follow-up query regardless of
        how many recordings are returned (no ProcessingJob/EdarRecord join at all -
        unlike GET /recordings/, this endpoint doesn't expose per-stage status)."""
        requesting_user = User.objects.get(user_id=params.user_id)
        recordings = (
            Recording.objects.filter(officer=requesting_user)
            .order_by('-created_at', '-recording_id')
            .values('recording_id', 'status', 'created_at')
        )
        data = [
            {
                'recordingId': r['recording_id'],
                'status': r['status'],
                'createdAt': r['created_at'].isoformat(),
            }
            for r in recordings
        ]
        return Response(status=status.HTTP_200_OK, data=Utils.success_response_data(message=self.data_get_all, data=data))

    @staticmethod
    def _build_list_items(recordings: list) -> list[dict]:
        """One extra query for ProcessingJob statuses and one for review_status,
        regardless of page size (docs/phase7-history-search.md §Performance) - never
        one query per recording."""
        recording_ids = [r.recording_id for r in recordings]
        if not recording_ids:
            return []

        latest_job_by_type: dict[tuple[int, str], ProcessingJob] = {}
        for job in ProcessingJob.objects.filter(recording_id__in=recording_ids).order_by(
            'recording_id', 'job_type', '-created_at'
        ):
            # First row per (recording_id, job_type) wins - already ordered newest
            # first, so this keeps only the most recent job per stage.
            latest_job_by_type.setdefault((job.recording_id, job.job_type), job)

        review_status_by_recording = dict(
            EdarRecord.objects.filter(recording_id__in=recording_ids).values_list('recording_id', 'review_status')
        )

        items = []
        for recording in recordings:
            stt_job = latest_job_by_type.get((recording.recording_id, 'STT'))
            translation_job = latest_job_by_type.get((recording.recording_id, 'TRANSLATION'))
            extraction_job = latest_job_by_type.get((recording.recording_id, 'EXTRACTION'))
            items.append({
                'recordingId': recording.recording_id,
                'status': recording.status,
                'createdAt': recording.created_at.isoformat(),
                'roadName': recording.road_name,
                'caseFirNumber': recording.case_fir_number,
                'policeStationJurisdiction': recording.police_station_jurisdiction,
                'processingStatus': stt_job.status if stt_job else None,
                'translationStatus': translation_job.status if translation_job else None,
                'extractionStatus': extraction_job.status if extraction_job else None,
                'reviewStatus': review_status_by_recording.get(recording.recording_id),
            })
        return items

    @staticmethod
    def _find_recent_duplicate(officer: User, checksum: str, exclude_recording_id: int) -> int | None:
        if not checksum:
            return None
        cutoff = timezone.now() - _DUPLICATE_DETECTION_WINDOW
        candidate = (
            Audio.objects.filter(checksum_sha256=checksum, recording__officer=officer, uploaded_at__gte=cutoff)
            .exclude(recording_id=exclude_recording_id)
            .order_by('-uploaded_at')
            .first()
        )
        return candidate.recording_id if candidate else None

    def _get_authorized_recording(self, requesting_user: User, recording_id: int) -> Recording:
        """Shared resource-level authorization (docs/phase2-sarvam-stt.md
        §Authorization, docs/open-decisions.md OD-007) - the owning officer, or any
        REVIEWER/ADMIN, may view or act on a recording; no other OFFICER may,
        regardless of whether they know the ID. Checked against the actual Recording
        row, never inferred from the URL alone. Used by both GET (view) and the
        Phase 6 approval endpoint (view + approve) - one authorization check, not
        two independently-maintained copies."""
        recording = Recording.objects.select_related('officer').filter(recording_id=recording_id).first()
        if recording is None:
            raise ValueError(self.recording_not_found)

        is_owner = recording.officer_id == requesting_user.user_id
        is_privileged = requesting_user.role in ('REVIEWER', 'ADMIN')
        if not (is_owner or is_privileged):
            raise ValueError(self.not_allowed)
        return recording

    @Common(response_handler=RecordingDetailResponseSerializer).exception_handler
    def get_extract(self, params: GetRecordingRequest, recording_id: int) -> Response:
        """Strictly read-only (docs/phase3-sarvam-translation.md §No provider call
        from GET) - never calls Sarvam, never starts/retries processing, never
        mutates any row. Only reads whatever csc_apps.processing.stt_service and
        csc_apps.processing.translation_service have already persisted."""
        requesting_user = User.objects.get(user_id=params.user_id)
        recording = self._get_authorized_recording(requesting_user, recording_id)
        return self._build_detail_response(recording)

    @Common(response_handler=RecordingDetailResponseSerializer).exception_handler
    def approve_edar_extract(self, params: ApproveEdarRequest, recording_id: int) -> Response:
        """Officer review + approval (docs/phase6-officer-review-approval.md).
        Field-level editing + whole-record approval, atomically: the officer's
        `params.fields` overrides are applied on top of the current AI candidate
        and validated, then a complete APPROVED EdarFieldValue snapshot is created
        in one transaction (csc_apps.edar.approval_service). AI rows are never
        read for anything but comparison - never updated, deleted, or re-tagged."""
        requesting_user = User.objects.get(user_id=params.user_id)
        recording = self._get_authorized_recording(requesting_user, recording_id)

        edar_record = EdarRecord.objects.filter(recording=recording).first()
        if edar_record is None:
            raise ValueError('No AI eDAR candidate exists for this recording')

        approval_service.approve_edar(edar_record=edar_record, officer=requesting_user, edits=params.fields)

        return self._build_detail_response(recording)

    @Common(response_handler=RecordingExportResponseSerializer).exception_handler
    def export_edar_extract(self, params: ExportEdarRequest, recording_id: int) -> Response:
        """Phase 8 export (docs/phase8-export.md). Read-only, same authorization as
        GET/<id>/ - reads `layer='APPROVED'` only (csc_apps.edar.export_service);
        never falls back to `layer='AI'` for anything. Rejects (ExportNotAvailable,
        a ValueError, mapped to the standard 400) unless review_status is exactly
        APPROVED."""
        requesting_user = User.objects.get(user_id=params.user_id)
        recording = self._get_authorized_recording(requesting_user, recording_id)

        edar_record = EdarRecord.objects.filter(recording=recording).first()
        edar_export = export_service.build_export(edar_record)

        # Phase 9 audit coverage (docs/phase9-security-audit-observability.md
        # §Export auditing, deferred by Phase 8) - identifies who exported which
        # recording's approved eDAR and when. Never the exported payload itself -
        # `details` carries only IDs and counts, the same value-free convention
        # csc_apps.edar.approval_service already uses for its own audit entries.
        ActivityLog.record(
            user=requesting_user, action='Read', model='EdarRecord',
            details={
                'event': 'export', 'recording_id': recording.recording_id,
                'edar_record_id': edar_record.edar_record_id,
            },
        )

        gps_coordinates = None
        if recording.gps_latitude is not None and recording.gps_longitude is not None:
            gps_coordinates = {
                'latitude': recording.gps_latitude,
                'longitude': recording.gps_longitude,
                'capturedAt': recording.gps_captured_at.isoformat() if recording.gps_captured_at else None,
            }

        return Response(
            status=status.HTTP_200_OK,
            data=Utils.success_response_data(
                message=self.data_export,
                data={
                    'recordingId': recording.recording_id,
                    'caseFirNumber': recording.case_fir_number,
                    'reviewStatus': edar_record.review_status,
                    'approvedBy': (
                        {
                            'userId': edar_record.reviewed_by_id,
                            'name': edar_record.reviewed_by.name,
                            'email': edar_record.reviewed_by.email,
                        }
                        if edar_record.reviewed_by_id else None
                    ),
                    'approvedAt': edar_record.reviewed_at.isoformat() if edar_record.reviewed_at else None,
                    'gpsCoordinates': gps_coordinates,
                    'eDAR': edar_export,
                },
            ),
        )

    def _build_detail_response(self, recording: Recording) -> Response:
        """Shared response builder for GET and the approval endpoint - both return
        the exact same recording-detail shape (docs/phase6-officer-review-
        approval.md §Response design: no new response format), so approval simply
        reads back the state GET would show immediately afterward, now with
        `edar.reviewStatus`/`edar.approved` populated."""
        stt_job = ProcessingJob.objects.filter(recording=recording, job_type='STT').order_by('-created_at').first()
        processing_status = stt_job.status if stt_job else 'PENDING'

        original_data = None
        stt_failure_reason = None
        if processing_status == 'SUCCEEDED':
            original_row = Transcript.objects.filter(recording=recording, language='ORIGINAL').first()
            if original_row is not None:
                original_data = {'text': original_row.text, 'language': original_row.detected_language_code}
        elif processing_status == 'FAILED':
            stt_failure_reason = stt_job.error_code

        translation_job = (
            ProcessingJob.objects.filter(recording=recording, job_type='TRANSLATION').order_by('-created_at').first()
        )
        translation_status = translation_job.status if translation_job else None

        english_data = None
        translation_failure_reason = None
        if translation_status == 'SUCCEEDED':
            english_row = Transcript.objects.filter(recording=recording, language='ENGLISH').first()
            if english_row is not None:
                english_data = {'text': english_row.text, 'language': english_row.detected_language_code}
        elif translation_status == 'FAILED':
            translation_failure_reason = translation_job.error_code

        extraction_job = (
            ProcessingJob.objects.filter(recording=recording, job_type='EXTRACTION').order_by('-created_at').first()
        )
        extraction_status = extraction_job.status if extraction_job else None

        edar_data = None
        extraction_failure_reason = None
        extraction_issues = []
        if extraction_status == 'SUCCEEDED':
            edar_record = EdarRecord.objects.filter(recording=recording).first()
            if edar_record is not None:
                edar_data = self._build_edar_data(edar_record)
        elif extraction_status == 'FAILED':
            extraction_failure_reason = extraction_job.error_code
            report = (extraction_job.provider_metadata or {}).get('validation_report') or {}
            extraction_issues = report.get('errors', [])

        return Response(
            status=status.HTTP_200_OK,
            data=Utils.success_response_data(
                message=self._build_message(processing_status, translation_status, extraction_status),
                data={
                    'recordingId': recording.recording_id,
                    'processingStatus': processing_status,
                    'translationStatus': translation_status,
                    'extractionStatus': extraction_status,
                    'transcript': {'original': original_data, 'english': english_data},
                    'edar': edar_data,
                    'failureReason': stt_failure_reason,
                    'translationFailureReason': translation_failure_reason,
                    'extractionFailureReason': extraction_failure_reason,
                    'extractionIssues': extraction_issues,
                },
            ),
        )

    @staticmethod
    def _build_edar_data(edar_record) -> dict:
        """Read-only projection of the persisted AI candidate (docs/phase5-validation-
        provenance.md §API representation) - nothing here is recomputed or re-validated
        on read; it only reshapes what extraction already validated and stored. A
        record written before Phase 5 has no quality report; `quality` is then null
        rather than a fabricated status."""
        report = edar_record.quality_report or None
        warning_codes_by_field: dict[str, list[str]] = {}
        for item in (report or {}).get('warnings', []):
            if item.get('field'):
                warning_codes_by_field.setdefault(item['field'], []).append(item['code'])

        rows = list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI'))
        fields = {}
        for row in rows:
            codes = warning_codes_by_field.get(row.field_key, [])
            has_evidence = bool(row.source_transcript_segment)
            fields[row.field_key] = {
                'value': row.value,
                'known': row.known,
                # Model-generated signal, not a probability of correctness.
                'confidence': row.confidence,
                # A reference into the English transcript, never a second narrative.
                'evidence': row.source_transcript_segment,
                # null = no evidence or no report to judge by; true/false only when
                # traceability was actually checked at extraction time.
                'evidenceVerified': (
                    ('evidence_not_traceable' not in codes) if (has_evidence and report is not None) else None
                ),
                'warnings': codes,
            }

        quality = None
        if report is not None:
            quality = {
                'status': edar_record.quality_status or report.get('status'),
                'errors': report.get('errors', []),
                'warnings': report.get('warnings', []),
                'metrics': report.get('metrics', {}),
            }

        # Additive over Phase 5's shape (docs/phase6-officer-review-approval.md §GET
        # behavior after approval) - `fields` above is untouched, still exactly the
        # AI candidate; `approved` is null until an officer approves, never
        # replacing the AI view above it.
        approved = None
        if edar_record.review_status == 'APPROVED':
            approved_rows = EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED')
            approved = {
                'reviewedBy': (
                    {
                        'userId': edar_record.reviewed_by_id,
                        'name': edar_record.reviewed_by.name,
                        'email': edar_record.reviewed_by.email,
                    }
                    if edar_record.reviewed_by_id else None
                ),
                'reviewedAt': edar_record.reviewed_at.isoformat() if edar_record.reviewed_at else None,
                'fields': {row.field_key: {'value': row.value, 'known': row.known} for row in approved_rows},
            }

        return {
            'layer': 'AI',
            'reviewStatus': edar_record.review_status,
            'quality': quality,
            'provenance': {
                'sourceTranscriptLanguage': 'ENGLISH' if edar_record.source_transcript_id else None,
                'extractionVersion': rows[0].extraction_version if rows else None,
                'extractedAt': edar_record.extracted_at.isoformat() if edar_record.extracted_at else None,
            },
            'fields': fields,
            'approved': approved,
        }

    @staticmethod
    def _build_message(processing_status: str, translation_status: str | None, extraction_status: str | None) -> str:
        if processing_status == 'FAILED':
            return 'Processing failed'
        if processing_status != 'SUCCEEDED':
            return 'Processing not yet complete'
        # STT succeeded from here on.
        if translation_status == 'FAILED':
            return 'Original transcript retrieved; translation failed'
        if translation_status in ('PENDING', 'RUNNING', 'RETRYING'):
            return 'Original transcript retrieved; translation in progress'
        if translation_status != 'SUCCEEDED':
            return 'Transcript retrieved successfully'
        # Translation succeeded from here on.
        if extraction_status == 'SUCCEEDED':
            return 'Recording transcripts and AI eDAR candidate retrieved successfully'
        if extraction_status == 'FAILED':
            return 'Recording transcripts retrieved successfully; eDAR extraction failed'
        if extraction_status in ('PENDING', 'RUNNING', 'RETRYING'):
            return 'Recording transcripts retrieved successfully; eDAR extraction in progress'
        return 'Recording transcripts retrieved successfully'
