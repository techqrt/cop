from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from csc_apps.common.serializer_validations import SerializerValidations
from csc_apps.recordings.serializers.request.get_recording import GetRecordingRequestSerializer
from csc_apps.recordings.serializers.request.upload_recording import RecordingUploadRequestSerializer
from csc_apps.recordings.serializers.response.get_recording import RecordingDetailResponseSerializer
from csc_apps.recordings.serializers.response.upload_recording import RecordingUploadResponseSerializer
from csc_apps.recordings.views import RecordingView


class RecordingViewController:

    @extend_schema(
        description=(
            'Upload a crash-scene audio recording (multipart/form-data). Creates the '
            'Recording and Audio rows, stores the audio privately, and queues a '
            'PENDING ProcessingJob for a future phase to consume - no transcription, '
            'translation, or extraction happens here (docs/phase1-audio-ingestion.md).'
        ),
        request={'multipart/form-data': RecordingUploadRequestSerializer},
        parameters=[
            OpenApiParameter(
                name='Authorization', type=str, location=OpenApiParameter.HEADER,
                required=True, description='Bearer <token>',
            ),
        ],
        responses={201: RecordingUploadResponseSerializer},
    )
    @api_view(['POST'])
    @SerializerValidations(serializer=RecordingUploadRequestSerializer).validate
    def upload(request: Request) -> Response:
        # DRF's default parser classes already include MultiPartParser, so
        # multipart/form-data (the audio file field) works without any override here.
        return RecordingView().upload_extract(params=request.params)

    @extend_schema(
        description=(
            'Read-only. Get a recording\'s STT status (`processingStatus`), translation '
            'status (`translationStatus`), eDAR extraction status (`extractionStatus`), '
            'both transcript versions, and the AI-candidate eDAR field data once '
            'available (docs/phase2-sarvam-stt.md §API endpoint, '
            'docs/phase3-sarvam-translation.md §API response, '
            'docs/phase4-gemini-edar-extraction.md §API behavior). Requires the caller '
            'to be the recording\'s owning officer, or a REVIEWER/ADMIN. Never calls '
            'Sarvam or Gemini and never starts or retries processing - this endpoint '
            'only reads already-persisted state.\n\n'
            '`transcript.original`/`transcript.english`/`edar` are each `null` until '
            'their respective stage succeeds - never fabricated while processing is '
            'incomplete. `translationStatus`/`extractionStatus` are `null` until the '
            'preceding stage has succeeded (there is nothing to translate/extract '
            'before that). `edar.fields` is a flat map of eDAR field_key -> '
            '{value, known, confidence} for every field Gemini attempted - `known` is '
            '"KNOWN" (evidence found) or "UNKNOWN" (attempted, none found); a value is '
            'never fabricated for an UNKNOWN field. `edar.layer` is always "AI" in '
            'Phase 4 - officer-approved data is a later phase. `failureReason`/'
            '`translationFailureReason`/`extractionFailureReason` each expose only a '
            'controlled error code, never the raw provider error message.'
        ),
        parameters=[
            OpenApiParameter(
                name='Authorization', type=str, location=OpenApiParameter.HEADER,
                required=True, description='Bearer <token>',
            ),
        ],
        responses={200: RecordingDetailResponseSerializer},
    )
    @api_view(['GET'])
    @SerializerValidations(serializer=GetRecordingRequestSerializer).validate
    def get(request: Request, recording_id: int) -> Response:
        return RecordingView().get_extract(params=request.params, recording_id=recording_id)
