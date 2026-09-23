import dataclasses


@dataclasses.dataclass
class GetAllRecordingsRequest:
    """Empty on purpose - no path/query parameters at all (docs/phase10a-get-all-
    and-smoke-test.md). Same convention as GetRecordingRequest/ExportEdarRequest:
    exists only so SerializerValidations has a dataclass to attach user_id to."""
