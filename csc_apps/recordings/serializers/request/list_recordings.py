from rest_framework import serializers

from csc.config import Configurations
from csc_apps.edar.models import EdarRecord
from csc_apps.recordings.dataclasses.request.list_recordings import ListRecordingsRequest
from csc_apps.recordings.models.recording import Recording


class ListRecordingsRequestSerializer(serializers.Serializer):
    # PMS's established pagination convention (csc_apps.common.utils.Utils.
    # add_page_parameter, Configurations.pagination_count) - not a new protocol.
    page_num = serializers.IntegerField(required=False, default=1, min_value=1)
    limit = serializers.IntegerField(
        required=False, default=Configurations.pagination_count, min_value=1, max_value=100
    )
    # Whitelisted against the existing enums - no client-supplied field name or
    # arbitrary value ever reaches the query (docs/phase7-history-search.md §Query
    # safety).
    status = serializers.ChoiceField(
        choices=[c[0] for c in Recording.STATUS_CHOICES], required=False, allow_null=True, default=None
    )
    review_status = serializers.ChoiceField(
        choices=[c[0] for c in EdarRecord.REVIEW_STATUS_CHOICES], required=False, allow_null=True, default=None
    )
    # Recording's own officer-confirmed columns (Phase 1), not eDAR-derived -
    # case_fir_number is a precise identifier (exact match); road_name is free
    # text an officer may only partly remember (case-insensitive partial match,
    # applied in the view).
    road_name = serializers.CharField(required=False, allow_null=True, allow_blank=False, max_length=255, default=None)
    case_fir_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=False, max_length=100, default=None
    )
    # Recording creation date, not crash date - docs/phase7-history-search.md
    # §Date filters explains why crash-date filtering is out of scope.
    created_from = serializers.DateField(required=False, allow_null=True, default=None)
    created_to = serializers.DateField(required=False, allow_null=True, default=None)

    def create(self, validated_data) -> ListRecordingsRequest:
        return ListRecordingsRequest(**validated_data)
