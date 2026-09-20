from rest_framework import serializers


class RecordingUploadDataSerializer(serializers.Serializer):
    recordingId = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    audioId = serializers.IntegerField(read_only=True)
    processingJobId = serializers.IntegerField(read_only=True)
    # Set when a very recent (see csc_apps.recordings.views) identical-checksum
    # upload from the same officer already exists - a lightweight, safe-minimum
    # signal for "this looks like a duplicate submission" without blocking the
    # request or attempting distributed idempotency-key deduplication
    # (docs/open-decisions.md OD-011).
    possibleDuplicateOfRecordingId = serializers.IntegerField(read_only=True, allow_null=True)


class RecordingUploadResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key
    (docs/pms-reference-analysis.md §6)."""

    data = RecordingUploadDataSerializer(read_only=True)
