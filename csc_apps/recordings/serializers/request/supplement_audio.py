from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.supplement_audio import SupplementAudioRequest


class SupplementAudioRequestSerializer(serializers.Serializer):
    audio = serializers.FileField(required=True, allow_empty_file=False)

    def create(self, validated_data) -> SupplementAudioRequest:
        return SupplementAudioRequest(**validated_data)
