from rest_framework import serializers


class RecordingListItemSerializer(serializers.Serializer):
    """One concise history row (docs/phase7-history-search.md §Response) - never
    transcript text, full eDAR fields, quality report, or audit details; those stay
    on GET /recordings/<id>/. `roadName`/`caseFirNumber`/`policeStationJurisdiction`
    are Recording's own officer-confirmed columns (Phase 1), not an eDAR AI/APPROVED
    value - no layer ambiguity to represent here."""

    recordingId = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    createdAt = serializers.CharField(read_only=True)
    roadName = serializers.CharField(read_only=True, allow_null=True)
    caseFirNumber = serializers.CharField(read_only=True, allow_null=True)
    policeStationJurisdiction = serializers.CharField(read_only=True, allow_null=True)
    # Same meaning as the equivalent fields on GET /recordings/<id>/ - the latest
    # ProcessingJob status per stage, null until that job exists.
    processingStatus = serializers.CharField(read_only=True, allow_null=True)
    translationStatus = serializers.CharField(read_only=True, allow_null=True)
    extractionStatus = serializers.CharField(read_only=True, allow_null=True)
    # EdarRecord.review_status verbatim - null until an AI eDAR candidate exists.
    # APPROVED here means exactly what it means on GET/<id>/approve/ - an
    # immutable human-approved snapshot exists; never conflated with the AI value.
    reviewStatus = serializers.CharField(read_only=True, allow_null=True)


class RecordingListDataSerializer(serializers.Serializer):
    """Same pagination envelope shape as csc_apps.common.utils.Utils.
    add_page_parameter already produces (ported from PMS) - not a new protocol."""

    data = RecordingListItemSerializer(many=True, read_only=True)
    presentPage = serializers.IntegerField(read_only=True)
    totalPage = serializers.IntegerField(read_only=True)
    nextPageUrl = serializers.CharField(read_only=True, required=False)
    previousPageUrl = serializers.CharField(read_only=True, required=False)


class RecordingListResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key
    (docs/pms-reference-analysis.md §6)."""

    data = RecordingListDataSerializer(read_only=True)
