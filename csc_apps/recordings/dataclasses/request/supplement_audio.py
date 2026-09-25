import dataclasses


@dataclasses.dataclass
class SupplementAudioRequest:
    """Audio only (docs/phase10b-supplemental-audio.md §API design) - deliberately
    no `fields`/`target_fields`/`missing_fields` parameter of any kind. Which eDAR
    fields this upload can help resolve is determined entirely server-side, from
    the recording's own current AI eDAR state (csc_apps.processing.
    extraction_service._run_targeted_extraction), never from anything the client
    sends."""

    audio: object  # django.core.files.uploadedfile.UploadedFile
