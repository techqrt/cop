import dataclasses


@dataclasses.dataclass
class ExportEdarRequest:
    """Empty on purpose - recording_id comes from the URL path, not the request
    body (same convention as GetRecordingRequest/ApproveEdarRequest). No client
    input at all - the export has a fixed contract, no arbitrary field/format
    selection (docs/phase8-export.md §Security)."""
