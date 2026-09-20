from csc_apps.recordings.storage.base import AudioStorage, AudioStorageError, StoredAudio
from csc_apps.recordings.storage.local import LocalPrivateAudioStorage

__all__ = ['AudioStorage', 'AudioStorageError', 'StoredAudio', 'LocalPrivateAudioStorage', 'get_storage']


def get_storage() -> AudioStorage:
    """Phase 1 has exactly one backend (docs/open-decisions.md OD-008 remains open for
    production). Centralized here so a future backend switch is a one-line change,
    not a call-site hunt."""
    return LocalPrivateAudioStorage()
