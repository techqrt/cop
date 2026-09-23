from rest_framework import serializers

from csc_apps.edar.models import EdarFieldValue
from csc_apps.recordings.dataclasses.request.approve_edar import ApproveEdarRequest


class ApprovedFieldEditSerializer(serializers.Serializer):
    """One field the officer is changing. No confidence/evidence/extraction_version
    here - those are AI provenance the client never submits (source instructions
    §24); the server determines approving officer and timestamp itself, never from
    the request body (§14, §41)."""

    known = serializers.ChoiceField(choices=[c[0] for c in EdarFieldValue.KNOWN_CHOICES])
    value = serializers.JSONField(required=False, allow_null=True, default=None)


class ApproveEdarRequestSerializer(serializers.Serializer):
    # Partial edit set, keyed by eDAR field_key - every field_key not present here
    # is carried through from the AI candidate unchanged
    # (docs/phase6-officer-review-approval.md §Request shape). Empty/omitted means
    # "approve the AI candidate as-is" (acceptance test: no-edit approval).
    fields = serializers.DictField(child=ApprovedFieldEditSerializer(), required=False, default=dict)

    def create(self, validated_data) -> ApproveEdarRequest:
        return ApproveEdarRequest(**validated_data)
