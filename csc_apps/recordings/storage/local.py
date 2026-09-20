import hashlib
import os
import uuid
from pathlib import Path

from django.conf import settings

from csc_apps.recordings.storage.base import AudioStorage, AudioStorageError, StoredAudio

_CHUNK_SIZE = 1024 * 1024


class LocalPrivateAudioStorage(AudioStorage):
    """Writes under settings.PRIVATE_STORAGE_ROOT - a directory that is never mounted
    under STATIC_URL/MEDIA_URL and never reachable through Django's static() dev
    helper (docs/security-baseline.md §3). This is Phase 1's only backend: a private,
    dependency-free local filesystem implementation for development, staging, and
    tests. A production cloud backend behind the same AudioStorage interface remains
    docs/open-decisions.md OD-008 - swapping it in later requires no domain-model or
    call-site change.

    Path shape: recordings/{recording_id}/audio/{uuid4}{extension}
    (docs/architecture-decisions.md, storage-key convention) - collision-resistant,
    never derived from the client-supplied filename (csc_apps.recordings.validators).
    """

    def _root(self) -> Path:
        root = Path(settings.PRIVATE_STORAGE_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def save(self, recording_id: int, extension: str, fileobj) -> StoredAudio:
        relative_path = f'recordings/{recording_id}/audio/{uuid.uuid4().hex}{extension}'
        target = self._root() / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(target.name + '.part')

        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp_path, 'wb') as out:
                chunks = fileobj.chunks(chunk_size=_CHUNK_SIZE) if hasattr(fileobj, 'chunks') else iter(
                    lambda: fileobj.read(_CHUNK_SIZE), b''
                )
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            # Atomic within the same filesystem - no reader ever observes a
            # partially-written file at the final path.
            os.replace(tmp_path, target)
        except OSError as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise AudioStorageError(f'Failed to store audio: {e}') from e

        return StoredAudio(storage_path=relative_path, size_bytes=size, checksum_sha256=digest.hexdigest())

    def delete(self, storage_path: str) -> None:
        target = self._root() / storage_path
        target.unlink(missing_ok=True)

    def open(self, storage_path: str):
        try:
            return open(self._root() / storage_path, 'rb')
        except OSError as e:
            raise AudioStorageError(f'Failed to open stored audio: {e}') from e
