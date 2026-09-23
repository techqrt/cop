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


class EdarIssueSerializer(serializers.Serializer):
    """One structured validation issue. `message` is a generic, value-free description
    - never the offending value, transcript text, or a provider error."""

    field = serializers.CharField(read_only=True, allow_null=True)
    code = serializers.CharField(read_only=True)
    severity = serializers.CharField(read_only=True)
    message = serializers.CharField(read_only=True)


class EdarQualitySerializer(serializers.Serializer):
    """Deterministic validation outcome of the AI candidate (docs/phase5-validation-
    provenance.md). status VALIDATED/VALIDATION_WARNING describes validation only -
    it is NOT approval and NOT a measure of factual accuracy; `metrics` are counts and a
    field-coverage ratio, deliberately with no accuracy or aggregate confidence score."""

    status = serializers.CharField(read_only=True)
    errors = EdarIssueSerializer(many=True, read_only=True)
    warnings = EdarIssueSerializer(many=True, read_only=True)
    metrics = serializers.DictField(child=serializers.JSONField(), read_only=True)


class EdarProvenanceSerializer(serializers.Serializer):
    sourceTranscriptLanguage = serializers.CharField(read_only=True, allow_null=True)
    extractionVersion = serializers.CharField(read_only=True, allow_null=True)
    extractedAt = serializers.CharField(read_only=True, allow_null=True)


class EdarFieldDataSerializer(serializers.Serializer):
    """One eDAR field's AI-layer state (docs/phase5-validation-provenance.md §API
    representation). `confidence` is a model-generated signal, not a probability of
    truth; `evidence` is a reference into the English transcript."""

    value = serializers.JSONField(read_only=True, allow_null=True)
    known = serializers.CharField(read_only=True)
    confidence = serializers.FloatField(read_only=True, allow_null=True)
    evidence = serializers.CharField(read_only=True, allow_null=True)
    evidenceVerified = serializers.BooleanField(read_only=True, allow_null=True)
    warnings = serializers.ListField(child=serializers.CharField(), read_only=True)


class ApprovedFieldDataSerializer(serializers.Serializer):
    """One eDAR field's APPROVED-layer state (docs/phase6-officer-review-approval.md
    §API representation). No confidence/evidence here - that provenance belongs to
    the AI layer (`edar.fields`), reachable from the same field_key; the APPROVED
    layer is the officer's asserted value, not a model-generated signal."""

    value = serializers.JSONField(read_only=True, allow_null=True)
    known = serializers.CharField(read_only=True)


class ReviewedBySerializer(serializers.Serializer):
    userId = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    email = serializers.CharField(read_only=True)


class ApprovedEdarSerializer(serializers.Serializer):
    """Null until an officer approves (docs/phase6-officer-review-approval.md
    §GET behavior after approval) - never fabricated, same "null while absent"
    convention as `transcript.english`/`edar` itself before their stage completes."""

    reviewedBy = ReviewedBySerializer(read_only=True)
    reviewedAt = serializers.CharField(read_only=True, allow_null=True)
    fields = serializers.DictField(child=ApprovedFieldDataSerializer(), read_only=True)


class EdarDataSerializer(serializers.Serializer):
    layer = serializers.CharField(read_only=True)
    # PENDING_REVIEW/IN_REVIEW/APPROVED - EdarRecord.review_status verbatim, no new
    # convention (docs/phase6-officer-review-approval.md §Review status).
    reviewStatus = serializers.CharField(read_only=True)
    quality = EdarQualitySerializer(read_only=True, allow_null=True)
    provenance = EdarProvenanceSerializer(read_only=True)
    fields = serializers.DictField(child=EdarFieldDataSerializer(), read_only=True)
    approved = ApprovedEdarSerializer(read_only=True, allow_null=True)


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
    # Structured, value-free validation errors when extraction failed validation.
    extractionIssues = EdarIssueSerializer(many=True, read_only=True)


class RecordingDetailResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key
    (docs/pms-reference-analysis.md §6)."""

    data = RecordingDetailDataSerializer(read_only=True)
