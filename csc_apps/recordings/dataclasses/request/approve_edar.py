import dataclasses


@dataclasses.dataclass
class ApproveEdarRequest:
    """`recording_id` comes from the URL path, not the body (same convention as
    GetRecordingRequest). `fields` is the officer's partial edit set - a field_key
    absent from it is copied through from the AI candidate unchanged
    (docs/phase6-officer-review-approval.md §Request shape)."""

    fields: dict
