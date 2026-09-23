import dataclasses
import datetime


@dataclasses.dataclass
class ListRecordingsRequest:
    """History/search over the authenticated user's own recordings
    (docs/phase7-history-search.md). All filters are optional and combine with
    AND semantics - no filter means no restriction on that dimension."""

    page_num: int = 1
    limit: int = 10
    status: str | None = None
    review_status: str | None = None
    road_name: str | None = None
    case_fir_number: str | None = None
    created_from: datetime.date | None = None
    created_to: datetime.date | None = None
