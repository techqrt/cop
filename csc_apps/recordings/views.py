import datetime as dt

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.common.common import Common
from csc_apps.common.utils import Utils
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing import event_types
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.recordings.dataclasses.request.get_recording import GetRecordingRequest
from csc_apps.recordings.dataclasses.request.upload_recording import UploadRecordingRequest
from csc_apps.recordings.models.audio import Audio, Transcript
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.serializers.response.get_recording import RecordingDetailResponseSerializer
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

    @Common(response_handler=RecordingDetailResponseSerializer).exception_handler
    def get_extract(self, params: GetRecordingRequest, recording_id: int) -> Response:
        """Strictly read-only (docs/phase3-sarvam-translation.md §No provider call
        from GET) - never calls Sarvam, never starts/retries processing, never
        mutates any row. Only reads whatever csc_apps.processing.stt_service and
        csc_apps.processing.translation_service have already persisted."""
        requesting_user = User.objects.get(user_id=params.user_id)
        recording = Recording.objects.select_related('officer').filter(recording_id=recording_id).first()
        if recording is None:
            raise ValueError(self.recording_not_found)

        # Resource-level authorization (docs/phase2-sarvam-stt.md §Authorization,
        # docs/open-decisions.md OD-007) - the owning officer, or any REVIEWER/ADMIN,
        # may view a recording; no other OFFICER may, regardless of whether they know
        # the ID. Checked against the actual Recording row, never inferred from the
        # URL alone. Unchanged from Phase 2 - covers both transcript versions, since
        # they're both reached through this same one authorization check.
        is_owner = recording.officer_id == requesting_user.user_id
        is_privileged = requesting_user.role in ('REVIEWER', 'ADMIN')
        if not (is_owner or is_privileged):
            raise ValueError(self.not_allowed)

        stt_job = ProcessingJob.objects.filter(recording=recording, job_type='STT').order_by('-created_at').first()
        processing_status = stt_job.status if stt_job else 'PENDING'

        original_data = None
        stt_failure_reason = None
        if processing_status == 'SUCCEEDED':
            original_row = Transcript.objects.filter(recording=recording, language='ORIGINAL').first()
            if original_row is not None:
                original_data = {'text': original_row.text, 'language': original_row.detected_language_code}
        elif processing_status == 'FAILED':
            # Only the controlled error_code is exposed, never the raw provider
            # error_message (docs/phase2-sarvam-stt.md §API response).
            stt_failure_reason = stt_job.error_code

        # translationStatus stays null until STT succeeds - translation is not
        # fabricated as "pending" before there is anything for it to be pending on
        # (docs/phase3-sarvam-translation.md §API response while translation is
        # pending).
        translation_job = (
            ProcessingJob.objects.filter(recording=recording, job_type='TRANSLATION')
            .order_by('-created_at')
            .first()
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

        # extractionStatus stays null until translation succeeds - same "nothing to
        # be pending on yet" reasoning as translationStatus above
        # (docs/phase4-gemini-edar-extraction.md §API response - extraction pending).
        extraction_job = (
            ProcessingJob.objects.filter(recording=recording, job_type='EXTRACTION')
            .order_by('-created_at')
            .first()
        )
        extraction_status = extraction_job.status if extraction_job else None

        edar_data = None
        extraction_failure_reason = None
        if extraction_status == 'SUCCEEDED':
            edar_record = EdarRecord.objects.filter(recording=recording).first()
            if edar_record is not None:
                field_rows = EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI')
                edar_data = {
                    'layer': 'AI',
                    'fields': {
                        row.field_key: {'value': row.value, 'known': row.known, 'confidence': row.confidence}
                        for row in field_rows
                    },
                }
        elif extraction_status == 'FAILED':
            extraction_failure_reason = extraction_job.error_code

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
                },
            ),
        )

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
