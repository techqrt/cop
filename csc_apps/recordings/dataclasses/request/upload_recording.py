import dataclasses


@dataclasses.dataclass
class UploadRecordingRequest:
    audio: object  # django.core.files.uploadedfile.UploadedFile
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    road_name: str | None = None
    police_station_jurisdiction: str | None = None
    case_fir_number: str | None = None
