from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.get_recording import GetRecordingRequest


class GetRecordingRequestSerializer(serializers.Serializer):
    def create(self, validated_data) -> GetRecordingRequest:
        return GetRecordingRequest()
