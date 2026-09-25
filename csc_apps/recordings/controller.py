from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from csc_apps.common.serializer_validations import SerializerValidations
from csc_apps.recordings.serializers.request.approve_edar import ApproveEdarRequestSerializer
from csc_apps.recordings.serializers.request.export_edar import ExportEdarRequestSerializer
from csc_apps.recordings.serializers.request.get_all_recordings import GetAllRecordingsRequestSerializer
from csc_apps.recordings.serializers.request.get_recording import GetRecordingRequestSerializer
from csc_apps.recordings.serializers.request.list_recordings import ListRecordingsRequestSerializer
from csc_apps.recordings.serializers.request.supplement_audio import SupplementAudioRequestSerializer
from csc_apps.recordings.serializers.request.upload_recording import RecordingUploadRequestSerializer
from csc_apps.recordings.serializers.response.export_edar import RecordingExportResponseSerializer
from csc_apps.recordings.serializers.response.get_all_recordings import RecordingGetAllResponseSerializer
from csc_apps.recordings.serializers.response.get_recording import RecordingDetailResponseSerializer
from csc_apps.recordings.serializers.response.list_recordings import RecordingListResponseSerializer
from csc_apps.recordings.serializers.response.upload_recording import RecordingUploadResponseSerializer
from csc_apps.recordings.views import RecordingView


_UPLOAD_DESCRIPTION = (
    'Upload a crash-scene audio recording (multipart/form-data). Creates the '
    'Recording and Audio rows, stores the audio privately, and queues a '
    'PENDING ProcessingJob for a future phase to consume - no transcription, '
    'translation, or extraction happens here (docs/phase1-audio-ingestion.md).'
)

_GET_DESCRIPTION = (
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
    'never fabricated for an UNKNOWN field. `edar.layer` is always "AI" - this '
    'is the AI candidate, never overwritten by approval (docs/phase6-officer-'
    'review-approval.md). `edar.reviewStatus` and `edar.approved` (null until '
    'approved) reflect Phase 6 review state. `failureReason`/'
    '`translationFailureReason`/`extractionFailureReason` each expose only a '
    'controlled error code, never the raw provider error message. All of the '
    'above continue to describe only the recording\'s ORIGINAL audio pipeline '
    '(docs/phase10b-supplemental-audio.md) even after a supplemental audio has '
    'been accepted via PUT on this same path - a supplemental audio\'s effect is '
    'visible only through a field flipping from UNKNOWN to KNOWN in `edar.fields`.'
)

_SUPPLEMENT_DESCRIPTION = (
    'Targeted supplemental audio for missing eDAR fields only '
    '(docs/phase10b-supplemental-audio.md). Accepts audio ONLY (multipart/'
    'form-data) - there is no `fields`/`target_fields`/`missing_fields` '
    'parameter of any kind; which eDAR fields this audio can help resolve is '
    'determined entirely server-side, from the recording\'s own current AI '
    'eDAR state, never from anything the client sends. Requires the recording '
    'to already have a completed AI eDAR extraction and not yet be approved '
    '(same authorization as GET - the owning officer, or a REVIEWER/ADMIN); '
    'rejected before any audio is stored if every eDAR field is already known. '
    'Repeated supplemental uploads are supported without limit, each '
    'accumulating whatever fields earlier ones left unresolved - an upload that '
    'resolves nothing is a valid, non-error outcome, never fabricated. '
    'Already-KNOWN AI fields and the APPROVED layer are never touched. Follows '
    'the same asynchronous pattern as the original upload (docs/phase1-audio-'
    'ingestion.md): this only stores the audio and queues a PENDING STT job, it '
    'never runs STT/translation/extraction synchronously. The response is the '
    'exact same shape GET returns.'
)

_LIST_DESCRIPTION = (
    'History/search over the authenticated user\'s own recordings '
    '(docs/phase7-history-search.md). Read-only, paginated, concise - one '
    'row per recording with lifecycle/processing/translation/extraction/'
    'review status and the recording\'s own road_name/case_fir_number/'
    'police_station_jurisdiction (Phase 1 officer-confirmed columns, not an '
    'eDAR AI/APPROVED value). Never returns transcript text, eDAR fields, '
    'quality reports, or audit details - use GET /recordings/<id>/ for full '
    'detail. Every role (officer, REVIEWER, ADMIN) sees only recordings '
    'they personally created - narrower than GET/<id>/\'s owner-OR-REVIEWER/'
    'ADMIN rule by design (docs/phase7-history-search.md §Authorization); a '
    'REVIEWER/ADMIN can still open any recording directly via GET/<id>/ if '
    'they already have the ID. All filters combine with AND semantics and '
    'are applied at the database query level before pagination.'
)


class RecordingViewController:

    @api_view(['POST'])
    @SerializerValidations(serializer=RecordingUploadRequestSerializer).validate
    def upload(request: Request) -> Response:
        # DRF's default parser classes already include MultiPartParser, so
        # multipart/form-data (the audio file field) works without any override here.
        return RecordingView().upload_extract(params=request.params)

    @api_view(['GET'])
    @SerializerValidations(serializer=GetRecordingRequestSerializer).validate
    def get(request: Request, recording_id: int) -> Response:
        return RecordingView().get_extract(params=request.params, recording_id=recording_id)

    @api_view(['PUT'])
    @SerializerValidations(serializer=SupplementAudioRequestSerializer).validate
    def supplement(request: Request, recording_id: int) -> Response:
        return RecordingView().supplement_extract(params=request.params, recording_id=recording_id)

    @extend_schema(
        description=(
            'Officer review + approval (docs/phase6-officer-review-approval.md). '
            'Creates the APPROVED eDAR snapshot: every field on the current AI '
            'candidate is copied through unchanged unless overridden by `fields` in '
            'the request body, which is validated against the same eDAR schema Phase '
            '4/5 use. AI EdarFieldValue rows are never modified, deleted, or '
            're-tagged - approval only ever creates `layer=APPROVED` rows. Requires '
            'the caller to be the recording\'s owning officer, or a REVIEWER/ADMIN '
            '(the same authorization GET uses). Fails with no APPROVED row written '
            'if: no AI eDAR candidate exists, the record is already approved, or an '
            'edit is invalid (unknown field_key, or a value that fails the eDAR '
            'schema\'s type/enum validation) - the whole operation is one database '
            'transaction. The response is the same shape GET returns, now with '
            '`edar.reviewStatus="APPROVED"` and `edar.approved` populated.'
        ),
        request=ApproveEdarRequestSerializer,
        parameters=[
            OpenApiParameter(
                name='Authorization', type=str, location=OpenApiParameter.HEADER,
                required=True, description='Bearer <token>',
            ),
        ],
        responses={200: RecordingDetailResponseSerializer},
    )
    @api_view(['POST'])
    @SerializerValidations(serializer=ApproveEdarRequestSerializer).validate
    def approve_edar(request: Request, recording_id: int) -> Response:
        return RecordingView().approve_edar_extract(params=request.params, recording_id=recording_id)

    @extend_schema(
        description=(
            'Export the officer-approved eDAR dataset as JSON (docs/phase8-export.md). '
            'Reads `layer=APPROVED` only - never falls back to the AI candidate for a '
            'missing value. Rejected (400) unless the recording has an eDAR record '
            'with `review_status="APPROVED"` (no eDAR extraction, a failed extraction, '
            'or an AI-only candidate all produce the same rejection). Same '
            'authorization as GET /recordings/<id>/ - the recording\'s owning officer, '
            'or a REVIEWER/ADMIN. The eDAR fields are grouped by the 7 eDAR modules '
            '(crashIdentification, roadEnvironment, crashCircumstances, vehicles[], '
            'casualties[], infrastructureObservations, officerAssessment); each field '
            'is `{known, value}` - known is one of KNOWN/UNKNOWN/NOT_APPLICABLE/'
            'UNCERTAIN, never silently converted to an empty value. `gpsCoordinates` '
            'is read from the Recording directly (device-captured, ADR-012 - it never '
            'had an AI/APPROVED eDAR row to begin with). No transcript, audio, or '
            'provider-internal data is included.'
        ),
        parameters=[
            OpenApiParameter(
                name='Authorization', type=str, location=OpenApiParameter.HEADER,
                required=True, description='Bearer <token>',
            ),
        ],
        responses={200: RecordingExportResponseSerializer},
    )
    @api_view(['GET'])
    @SerializerValidations(serializer=ExportEdarRequestSerializer).validate
    def export_edar(request: Request, recording_id: int) -> Response:
        return RecordingView().export_edar_extract(params=request.params, recording_id=recording_id)

    @extend_schema(
        description=(
            'Lightweight index of every recording accessible to the authenticated '
            'user (docs/phase10a-get-all-and-smoke-test.md). Deliberately distinct '
            'from GET /recordings/ (Phase 7): no pagination, no filters, no '
            'per-stage status breakdown - just `recordingId`, `status` '
            '(`Recording.status` verbatim, the one canonical lifecycle field), and '
            '`createdAt`. Every role, including REVIEWER/ADMIN, sees only '
            'recordings they personally created - the same list-context scoping '
            'GET /recordings/ already uses (docs/phase9-security-audit-'
            'observability.md §4). Never returns transcript, eDAR, audio, or '
            'provider data.'
        ),
        parameters=[
            OpenApiParameter(
                name='Authorization', type=str, location=OpenApiParameter.HEADER,
                required=True, description='Bearer <token>',
            ),
        ],
        responses={200: RecordingGetAllResponseSerializer},
    )
    @api_view(['GET'])
    @SerializerValidations(serializer=GetAllRecordingsRequestSerializer).validate
    def get_all(request: Request) -> Response:
        return RecordingView().get_all_extract(params=request.params)

    @api_view(['GET'])
    @SerializerValidations(serializer=ListRecordingsRequestSerializer).validate
    def list_recordings(request: Request) -> Response:
        return RecordingView().list_extract(params=request.params)

    _QUERY_PARAMETERS = [
        OpenApiParameter(name='page_num', type=int, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='limit', type=int, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='status', type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='review_status', type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='road_name', type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='case_fir_number', type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='created_from', type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name='created_to', type=str, location=OpenApiParameter.QUERY, required=False),
    ]

    # `POST /recordings/` (upload, Phase 1) and `GET /recordings/` (history/search,
    # Phase 7) share one URL path, per source instructions §5/§29/§30, rather than
    # a second `/history/` route. Django's `path()` matches by pattern only, not
    # HTTP method, so this single registered view documents and dispatches both -
    # `upload`/`list_recordings` above remain the real, independently-decorated
    # implementations (each still enforces its own single method, has its own
    # request/response serializer); this wrapper exists only because drf-
    # spectacular cannot introspect a plain undecorated dispatcher function - the
    # stacked `@extend_schema(methods=[...])` is drf-spectacular's own documented
    # mechanism for one function-based view serving different operations per
    # method.
    @extend_schema(methods=['POST'], operation_id='recording_upload', description=_UPLOAD_DESCRIPTION,
                    request={'multipart/form-data': RecordingUploadRequestSerializer},
                    parameters=[OpenApiParameter(name='Authorization', type=str, location=OpenApiParameter.HEADER,
                                                  required=True, description='Bearer <token>')],
                    responses={201: RecordingUploadResponseSerializer})
    @extend_schema(methods=['GET'], operation_id='recording_list', description=_LIST_DESCRIPTION,
                    parameters=[OpenApiParameter(name='Authorization', type=str, location=OpenApiParameter.HEADER,
                                                  required=True, description='Bearer <token>'), *_QUERY_PARAMETERS],
                    responses={200: RecordingListResponseSerializer})
    @api_view(['GET', 'POST'])
    def recordings_root(request: Request) -> Response:
        if request.method == 'GET':
            return RecordingViewController.list_recordings(request._request)
        return RecordingViewController.upload(request._request)

    # `GET /recordings/<id>/` (Phase 2-9) and `PUT /recordings/<id>/` (Phase 10B
    # supplemental audio) share one URL path, same reasoning and same drf-
    # spectacular mechanism as recordings_root above - `get`/`supplement` remain
    # the real, independently-decorated implementations; this wrapper exists only
    # so drf-spectacular can see both operations on one function-based view.
    @extend_schema(methods=['GET'], operation_id='recording_detail', description=_GET_DESCRIPTION,
                    parameters=[OpenApiParameter(name='Authorization', type=str, location=OpenApiParameter.HEADER,
                                                  required=True, description='Bearer <token>')],
                    responses={200: RecordingDetailResponseSerializer})
    @extend_schema(methods=['PUT'], operation_id='recording_supplement', description=_SUPPLEMENT_DESCRIPTION,
                    request={'multipart/form-data': SupplementAudioRequestSerializer},
                    parameters=[OpenApiParameter(name='Authorization', type=str, location=OpenApiParameter.HEADER,
                                                  required=True, description='Bearer <token>')],
                    responses={200: RecordingDetailResponseSerializer})
    @api_view(['GET', 'PUT'])
    def recording_detail_root(request: Request, recording_id: int) -> Response:
        if request.method == 'PUT':
            return RecordingViewController.supplement(request._request, recording_id=recording_id)
        return RecordingViewController.get(request._request, recording_id=recording_id)
