import dataclasses


@dataclasses.dataclass
class UploadRecordingRequest:
    # Optional as of Phase 10 (docs/phase10-live-voice-agent.md §Recording
    # creation): the live-voice workflow creates a Recording before any audio
    # exists at all (the conversation itself IS the audio, captured over
    # LiveKit, never uploaded as a file to this endpoint). When omitted,
    # RecordingView.upload_extract creates the Recording only, in CREATED
    # status, and skips the Audio/storage/STT-job machinery entirely - the
    # existing upload-with-audio path is otherwise byte-for-byte unchanged.
    audio: object | None = None  # django.core.files.uploadedfile.UploadedFile | None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    road_name: str | None = None
    police_station_jurisdiction: str | None = None
    case_fir_number: str | None = None
