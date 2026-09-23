from rest_framework import serializers

from csc_apps.recordings.serializers.response.get_recording import ReviewedBySerializer


class ExportFieldValueSerializer(serializers.Serializer):
    """One eDAR field's exported state - approved value only, no AI provenance
    (confidence/evidence/extraction_version) - the export represents the officer-
    approved result, not the AI's reasoning (docs/phase8-export.md §Provenance)."""

    known = serializers.CharField(read_only=True)
    value = serializers.JSONField(read_only=True, allow_null=True)


class ExportEdarDataSerializer(serializers.Serializer):
    """Module-grouped export shape (docs/phase8-export.md §Export schema) - the 7
    modules from eDAR Fields.pdf, with vehicles/casualties as arrays (one dict per
    approved vehicle/casualty record, not flattened dotted keys). gps_coordinates
    is intentionally absent here - see GpsCoordinatesSerializer below."""

    crashIdentification = serializers.DictField(child=ExportFieldValueSerializer(), read_only=True)
    roadEnvironment = serializers.DictField(child=ExportFieldValueSerializer(), read_only=True)
    crashCircumstances = serializers.DictField(child=ExportFieldValueSerializer(), read_only=True)
    vehicles = serializers.ListField(child=serializers.DictField(child=ExportFieldValueSerializer()), read_only=True)
    casualties = serializers.ListField(child=serializers.DictField(child=ExportFieldValueSerializer()), read_only=True)
    infrastructureObservations = serializers.DictField(child=ExportFieldValueSerializer(), read_only=True)
    officerAssessment = serializers.DictField(child=ExportFieldValueSerializer(), read_only=True)


class GpsCoordinatesSerializer(serializers.Serializer):
    """Device-captured (ADR-012) - read from Recording directly, never from the
    AI/APPROVED eDAR layer, which never represented gps_coordinates at all."""

    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)
    capturedAt = serializers.CharField(read_only=True, allow_null=True)


class RecordingExportDataSerializer(serializers.Serializer):
    recordingId = serializers.IntegerField(read_only=True)
    # Recording's own Phase 1 officer-confirmed column (docs/phase7-history-
    # search.md) - identifying metadata, not the eDAR-layer case_fir_number value
    # (that lives inside eDAR.crashIdentification.case_fir_number).
    caseFirNumber = serializers.CharField(read_only=True, allow_null=True)
    reviewStatus = serializers.CharField(read_only=True)
    approvedBy = ReviewedBySerializer(read_only=True, allow_null=True)
    approvedAt = serializers.CharField(read_only=True, allow_null=True)
    gpsCoordinates = GpsCoordinatesSerializer(read_only=True, allow_null=True)
    eDAR = ExportEdarDataSerializer(read_only=True)


class RecordingExportResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key - the
    same envelope every other endpoint uses, not a new file-download response
    (docs/phase8-export.md §Response shape; no Content-Disposition precedent
    exists anywhere in PMS either)."""

    data = RecordingExportDataSerializer(read_only=True)
