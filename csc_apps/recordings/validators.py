"""Backend audio-upload validation (docs/phase1-audio-ingestion.md §Validation).

Deliberately does not trust the client-supplied Content-Type alone: each accepted
format is cross-checked against a magic-byte signature read from the file itself, so
a mislabeled or malicious upload is rejected before it ever reaches storage. No new
dependency is introduced for this (no python-magic/libmagic) - the signature set below
covers exactly the formats in AUDIO_ALLOWED_CONTENT_TYPES and nothing more.

The extension used to build the storage key (csc_apps/recordings/storage/) always
comes from this canonical, allowlisted mapping - never from the client-supplied
filename - so the storage path can never be influenced by attacker-controlled input.
"""

import dataclasses
import os

from django.conf import settings

# content_type -> canonical, storage-safe extension. Interim allowlist
# (docs/open-decisions.md OD-010) - covers the formats common mobile
# recorders/officer devices produce; not exhaustive by design.
AUDIO_ALLOWED_CONTENT_TYPES: dict[str, str] = {
    'audio/mpeg': '.mp3',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
    'audio/wave': '.wav',
    'audio/mp4': '.m4a',
    'audio/x-m4a': '.m4a',
    'audio/aac': '.aac',
    'audio/ogg': '.ogg',
    'audio/webm': '.webm',
    'audio/flac': '.flac',
    'audio/x-flac': '.flac',
}

_SIGNATURE_HEAD_BYTES = 16


def _matches_wav(head: bytes) -> bool:
    return head[0:4] == b'RIFF' and head[8:12] == b'WAVE'


def _matches_mp3(head: bytes) -> bool:
    if head[0:3] == b'ID3':
        return True
    # Unsynced MPEG frame sync: 11 set bits (0xFFE.. through 0xFFF..).
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _matches_mp4_m4a(head: bytes) -> bool:
    return head[4:8] == b'ftyp'


def _matches_ogg(head: bytes) -> bool:
    return head[0:4] == b'OggS'


def _matches_webm(head: bytes) -> bool:
    return head[0:4] == b'\x1a\x45\xdf\xa3'


def _matches_flac(head: bytes) -> bool:
    return head[0:4] == b'fLaC'


# One check per canonical extension - multiple content-types can map to the same
# container format (e.g. audio/wav and audio/x-wav are both RIFF/WAVE).
_SIGNATURE_CHECKS = {
    '.wav': _matches_wav,
    '.mp3': _matches_mp3,
    '.m4a': _matches_mp4_m4a,
    '.aac': _matches_mp3,  # ADTS AAC shares the MPEG frame-sync pattern in practice.
    '.ogg': _matches_ogg,
    '.webm': _matches_webm,
    '.flac': _matches_flac,
}


@dataclasses.dataclass
class ValidatedAudio:
    extension: str
    original_filename: str | None


def validate_audio_upload(uploaded_file) -> ValidatedAudio:
    """Raises ValueError (caught by csc_apps.common.common.Common.exception_handler,
    same as every other domain validation failure - docs/pms-reference-analysis.md §8)
    describing the first violation found. Leaves `uploaded_file`'s read position at 0
    on return so the caller (storage layer) can read the full content from the start.
    """
    if uploaded_file.size <= 0:
        raise ValueError('Uploaded audio file is empty')

    max_size = settings.AUDIO_MAX_UPLOAD_SIZE_BYTES
    if uploaded_file.size > max_size:
        raise ValueError(
            f'Audio file is {uploaded_file.size} bytes, which exceeds the '
            f'{max_size}-byte limit (AUDIO_MAX_UPLOAD_SIZE_BYTES)'
        )

    content_type = (uploaded_file.content_type or '').split(';')[0].strip().lower()
    extension = AUDIO_ALLOWED_CONTENT_TYPES.get(content_type)
    if extension is None:
        raise ValueError(
            f'Unsupported audio content type "{content_type}". '
            f'Allowed: {sorted(set(AUDIO_ALLOWED_CONTENT_TYPES.values()))}'
        )

    uploaded_file.seek(0)
    head = uploaded_file.read(_SIGNATURE_HEAD_BYTES)
    uploaded_file.seek(0)

    signature_check = _SIGNATURE_CHECKS[extension]
    if not signature_check(head):
        raise ValueError(
            f'File content does not match the declared audio format ("{content_type}")'
        )

    original_filename = None
    if uploaded_file.name:
        # Never used to build a storage path (csc_apps/recordings/storage/) - kept
        # only as display/audit metadata, so sanitizing here is a defense-in-depth
        # courtesy, not a security boundary.
        original_filename = os.path.basename(uploaded_file.name)[:255]

    return ValidatedAudio(extension=extension, original_filename=original_filename)
