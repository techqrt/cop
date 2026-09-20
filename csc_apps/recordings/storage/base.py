"""Storage abstraction for raw audio evidence (docs/domain-model.md §3 note on
Audio.storage_path, ADR-002, docs/open-decisions.md OD-008). The application/domain
layer only ever talks to this interface - no concrete backend's client library or
credentials appear outside csc_apps/recordings/storage/<backend>.py.
"""

import abc
import dataclasses
from typing import BinaryIO


@dataclasses.dataclass
class StoredAudio:
    storage_path: str
    size_bytes: int
    checksum_sha256: str


class AudioStorageError(Exception):
    """Raised for any backend I/O failure - caught by
    csc_apps.common.common.Common.exception_handler's generic branch like any other
    server-side fault (docs/pms-reference-analysis.md §8)."""


class AudioStorage(abc.ABC):
    @abc.abstractmethod
    def save(self, recording_id: int, extension: str, fileobj) -> StoredAudio:
        """Writes `fileobj`'s full contents and returns where it landed plus its size
        and checksum. Must leave no partial artifact behind on failure (raises
        AudioStorageError instead)."""
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, storage_path: str) -> None:
        """Best-effort compensating delete - used when a later step in the same
        ingestion request fails after storage already succeeded
        (docs/phase1-audio-ingestion.md §Transaction boundary)."""
        raise NotImplementedError

    @abc.abstractmethod
    def open(self, storage_path: str) -> BinaryIO:
        """Read-only binary stream - for a future authenticated retrieval endpoint or
        Phase 2's STT provider to consume. Never a public URL (docs/security-baseline.md
        §3)."""
        raise NotImplementedError
