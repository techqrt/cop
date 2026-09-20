from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.upload_recording import UploadRecordingRequest


class RecordingUploadRequestSerializer(serializers.Serializer):
    audio = serializers.FileField(required=True, allow_empty_file=False)

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
