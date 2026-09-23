from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.get_all_recordings import GetAllRecordingsRequest


class GetAllRecordingsRequestSerializer(serializers.Serializer):
    def create(self, validated_data) -> GetAllRecordingsRequest:
        return GetAllRecordingsRequest()
