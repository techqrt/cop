from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.upload_recording import UploadRecordingRequest


class RecordingUploadRequestSerializer(serializers.Serializer):
    # required=False as of Phase 10 (docs/phase10-live-voice-agent.md §Recording
    # creation) - live-voice recordings are created with no audio file at all.
    # The existing audio-upload path's own validation (csc_apps.recordings.
    # validators.validate_audio_upload) is unchanged and still runs whenever
    # audio IS provided; this only widens what's accepted, never weakens it.
    audio = serializers.FileField(required=False, allow_empty_file=False, default=None)

    # Module A context an officer/client typically already has at recording-creation
    # time (docs/user-workflow.md §2 "Create Recording", docs/domain-model.md §3) -
    # all optional since none of it is guaranteed available (e.g. GPS indoors).
    gps_latitude = serializers.FloatField(required=False, allow_null=True, default=None)
    gps_longitude = serializers.FloatField(required=False, allow_null=True, default=None)
    road_name = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=255, default=None
    )
    police_station_jurisdiction = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=255, default=None
    )
    case_fir_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=100, default=None
    )

    def create(self, validated_data) -> UploadRecordingRequest:
        return UploadRecordingRequest(**validated_data)
