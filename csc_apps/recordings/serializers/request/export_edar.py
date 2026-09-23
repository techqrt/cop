from rest_framework import serializers

from csc_apps.recordings.dataclasses.request.export_edar import ExportEdarRequest


class ExportEdarRequestSerializer(serializers.Serializer):
    def create(self, validated_data) -> ExportEdarRequest:
        return ExportEdarRequest()
