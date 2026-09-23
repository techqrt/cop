from rest_framework import serializers


class RecordingSummarySerializer(serializers.Serializer):
    """One recording's lightweight index entry (docs/phase10a-get-all-and-smoke-
    test.md) - deliberately smaller than GET /recordings/'s per-stage status
    breakdown (Phase 7) and worlds smaller than GET /recordings/<id>/'s full
    transcript/eDAR detail. `status` is Recording.status verbatim - the one
    canonical lifecycle field (docs/recording-state-machine.md), not a second,
    competing status derived from ProcessingJob/ProcessingEvent."""

    recordingId = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    createdAt = serializers.CharField(read_only=True)


class RecordingGetAllResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key - a plain
    array here, not GET /recordings/'s paginated {data, presentPage, totalPage}
    object (docs/phase10a-get-all-and-smoke-test.md §Response shape - no
    pagination, by design: the endpoint's whole purpose is "all accessible
    recordings" in one call)."""

    data = RecordingSummarySerializer(many=True, read_only=True)
