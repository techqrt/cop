from rest_framework import serializers


class TranscriptDataSerializer(serializers.Serializer):
    text = serializers.CharField(read_only=True)
    language = serializers.CharField(read_only=True, allow_null=True)


class TranscriptPairSerializer(serializers.Serializer):
    """Both transcript versions (docs/phase3-sarvam-translation.md §API response) -
    each null independently of the other, since STT and translation are independent,
    independently-retryable stages (ADR-016, ADR-017)."""

    original = TranscriptDataSerializer(read_only=True, allow_null=True)
    english = TranscriptDataSerializer(read_only=True, allow_null=True)


class EdarFieldDataSerializer(serializers.Serializer):
    """One eDAR field's AI-layer state (docs/phase4-gemini-edar-extraction.md §API
    behavior). Deliberately minimal for Phase 4 - `value`/`known`/`confidence` only.
    Per-field provenance/evidence display is Phase 5's job (source instructions'
    stop condition), not exposed here even though it is already persisted on
    EdarFieldValue."""

    value = serializers.JSONField(read_only=True, allow_null=True)
    known = serializers.CharField(read_only=True)
    confidence = serializers.FloatField(read_only=True, allow_null=True)


class EdarDataSerializer(serializers.Serializer):
    layer = serializers.CharField(read_only=True)
    fields = serializers.DictField(child=EdarFieldDataSerializer(), read_only=True)


class RecordingDetailDataSerializer(serializers.Serializer):
    recordingId = serializers.IntegerField(read_only=True)
    # STT ProcessingJob status - unchanged meaning from Phase 2 (docs/phase3-sarvam-
    # translation.md §API response, a deliberately non-breaking extension).
    processingStatus = serializers.CharField(read_only=True)
    # TRANSLATION ProcessingJob status - null until STT has succeeded and a
    # translation job exists.
    translationStatus = serializers.CharField(read_only=True, allow_null=True)
    # EXTRACTION ProcessingJob status - null until translation has succeeded and an
    # extraction job exists (docs/phase4-gemini-edar-extraction.md §API behavior).
    extractionStatus = serializers.CharField(read_only=True, allow_null=True)
    transcript = TranscriptPairSerializer(read_only=True)
    # Null until extractionStatus == 'SUCCEEDED' - an AI eDAR candidate is never
    # fabricated while processing is incomplete, same pattern as transcript.english.
    edar = EdarDataSerializer(read_only=True, allow_null=True)
    # Only the controlled error_code (e.g. "STT_UNSUPPORTED_INPUT"), never the raw
    # provider error_message - docs/phase2-sarvam-stt.md §API response.
    failureReason = serializers.CharField(read_only=True, allow_null=True)
    translationFailureReason = serializers.CharField(read_only=True, allow_null=True)
    extractionFailureReason = serializers.CharField(read_only=True, allow_null=True)


class RecordingDetailResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key
    (docs/pms-reference-analysis.md §6)."""

    data = RecordingDetailDataSerializer(read_only=True)
