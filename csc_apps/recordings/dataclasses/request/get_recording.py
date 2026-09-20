import dataclasses


@dataclasses.dataclass
class GetRecordingRequest:
    """Empty on purpose - recording_id comes from the URL path, not the request body
    (docs/phase2-sarvam-stt.md §API endpoint). Exists only so
    csc_apps.common.serializer_validations.SerializerValidations has a dataclass to
    attach user_id to, same convention as every other endpoint."""
