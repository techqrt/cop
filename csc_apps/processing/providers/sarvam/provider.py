"""Sarvam Speech-to-Text integration, isolated behind SpeechToTextProvider
(docs/phase2-sarvam-stt.md §Provider abstraction). No Sarvam-specific type or
response shape leaves this module - callers only ever see TranscriptResult/
ProviderError from csc_apps.processing.providers.base.

Uses Sarvam's **Batch** Speech-to-Text API (job-based: create -> upload -> start ->
poll -> download) via the official `sarvamai` SDK, not the synchronous
`POST /speech-to-text` REST endpoint. Verified against https://docs.sarvam.ai
(2026-09): the synchronous endpoint caps audio at 30 seconds, while Phase 1 accepts
uploads well beyond that (crash-scene statements run minutes) - the Batch API (up to
2 hours/file) is the only documented endpoint that fits. The Batch API's raw "start
job" REST contract is not published in plain HTTP docs, only wrapped by the SDK
(`job.start()`), which is why this is an SDK integration rather than raw `requests`
calls (docs/phase2-sarvam-stt.md §Dependency rule) - the alternative would mean
guessing an unpublished endpoint, which the source instructions explicitly forbid.
"""

import json
import os
import tempfile

import httpx
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

from csc.config import Configurations
from csc_apps.processing.providers.base import ProviderError, SpeechToTextProvider, TranscriptResult

# Maps a Sarvam ApiError.status_code to one of the error codes
# csc_apps.processing.error_classification knows how to classify. Any status not
# listed falls back to STT_PROVIDER_UNAVAILABLE (retryable) - the safer default for
# an unrecognized server-side condition, since a client-input problem the API can
# already reject with 400/403/422 explicitly.
_STATUS_CODE_TO_ERROR_CODE = {
    400: 'STT_UNSUPPORTED_INPUT',
    403: 'STT_AUTHENTICATION_FAILED',
    422: 'STT_UNSUPPORTED_INPUT',
    429: 'STT_PROVIDER_RATE_LIMITED',
    500: 'STT_PROVIDER_UNAVAILABLE',
    503: 'STT_PROVIDER_UNAVAILABLE',
}


def _classify_api_error(error: ApiError) -> str:
    return _STATUS_CODE_TO_ERROR_CODE.get(error.status_code, 'STT_PROVIDER_UNAVAILABLE')


class SarvamSpeechToTextProvider(SpeechToTextProvider):
    PROVIDER_NAME = 'sarvam'
    MODEL = 'saaras:v3'

    def __init__(self):
        self._api_key = Configurations.sarvam_api_key
        self._http_timeout_seconds = Configurations.sarvam_http_timeout_seconds
        self._poll_interval_seconds = Configurations.sarvam_poll_interval_seconds
        self._poll_timeout_seconds = Configurations.sarvam_poll_timeout_seconds

    def _client(self) -> SarvamAI:
        if not self._api_key:
            # Fails the same way an actual 403 from Sarvam would, so the caller's
            # retry/error-classification path doesn't need a separate "not
            # configured" case (docs/phase2-sarvam-stt.md §Error handling).
            raise ProviderError('STT_AUTHENTICATION_FAILED', 'SARVAM_API_KEY is not configured')
        # Always pass the key explicitly (never rely on the SDK's own os.getenv('SARVAM_API_KEY')
        # fallback) so configuration flows through csc.config.Configurations only
        # (docs/pms-reference-analysis.md §1 configuration convention).
        return SarvamAI(api_subscription_key=self._api_key, timeout=self._http_timeout_seconds)

    def transcribe(self, local_audio_path: str, content_type: str) -> TranscriptResult:
        client = self._client()
        try:
            job = client.speech_to_text_job.create_job(
                model=self.MODEL,
                mode='transcribe',  # never 'translate' - no translation in Phase 2 (product decision E)
                with_diarization=False,  # ADR-005 - one recording, one speaker
                with_timestamps=False,  # not needed for Phase 2's plain-text transcript
                language_code=None,  # let Sarvam auto-detect ("unknown") - docs/phase2-sarvam-stt.md §Language handling
            )
            job.upload_files(file_paths=[local_audio_path], timeout=self._http_timeout_seconds)
            job.start()
            status = job.wait_until_complete(
                poll_interval=self._poll_interval_seconds, timeout=self._poll_timeout_seconds
            )
        except ApiError as e:
            raise ProviderError(_classify_api_error(e), f'Sarvam API error: {e.body}') from e
        except TimeoutError as e:
            # Our own poll ceiling was reached, not a Sarvam-reported failure - worth
            # retrying (the job may still complete server-side, or a fresh attempt
            # may run faster).
            raise ProviderError('STT_PROVIDER_TIMEOUT', str(e)) from e
        except httpx.TimeoutException as e:
            # A raw network-level timeout (connect/read/write) below the SDK's own
            # ApiError handling - the SDK only wraps HTTP-level error *responses*,
            # not transport failures, so these reach us as plain httpx exceptions.
            raise ProviderError('STT_PROVIDER_TIMEOUT', str(e)) from e
        except httpx.TransportError as e:
            # DNS failure, connection refused, connection reset, etc. - transient by
            # nature.
            raise ProviderError('STT_PROVIDER_UNAVAILABLE', str(e)) from e
        except RuntimeError as e:
            # Raised by the SDK itself for a non-2xx on a presigned upload/download
            # URL (blob storage, not the Sarvam API proper) - treated as a transient
            # storage-layer fault.
            raise ProviderError('STT_PROVIDER_UNAVAILABLE', str(e)) from e

        if status.job_state != 'Completed':
            raise ProviderError(
                'STT_UNSUPPORTED_INPUT',
                f'Sarvam job ended in state {status.job_state!r}: {status.error_message}',
            )

        file_results = job.get_file_results()
        if file_results['failed'] or not file_results['successful']:
            raise ProviderError(
                'STT_UNSUPPORTED_INPUT', f'Sarvam could not process the audio: {file_results["failed"]}'
            )

        raw_result = self._download_single_result(job)
        return self._map_to_canonical(raw_result)

    def _download_single_result(self, job) -> dict:
        # Exactly one file was uploaded (docs/phase2-sarvam-stt.md §Architecture - one
        # ProcessingJob = one Recording's Audio = one Sarvam job), so exactly one
        # output JSON is expected. Downloaded into a temp dir that is deleted
        # immediately after reading - never exposed, never treated as canonical
        # (docs/phase2-sarvam-stt.md §12 Immutable source audio, §Temporary files).
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                job.download_outputs(output_dir=tmp_dir)
            except RuntimeError as e:
                raise ProviderError('STT_PROVIDER_UNAVAILABLE', str(e)) from e

            output_files = [f for f in os.listdir(tmp_dir) if f.endswith('.json')]
            if len(output_files) != 1:
                raise ProviderError(
                    'STT_MALFORMED_RESPONSE', f'Expected exactly one Sarvam output file, got {output_files}'
                )
            output_path = os.path.join(tmp_dir, output_files[0])
            try:
                with open(output_path, encoding='utf-8') as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise ProviderError('STT_MALFORMED_RESPONSE', f'Could not parse Sarvam output file: {e}') from e

    def _map_to_canonical(self, raw: dict) -> TranscriptResult:
        transcript_text = raw.get('transcript')
        if not isinstance(transcript_text, str):
            raise ProviderError(
                'STT_MALFORMED_RESPONSE', f'"transcript" field missing or not a string: {type(transcript_text)}'
            )
        if not transcript_text.strip():
            raise ProviderError('STT_EMPTY_TRANSCRIPT', 'Sarvam returned an empty transcript for this audio')

        return TranscriptResult(
            text=transcript_text,
            detected_language_code=raw.get('language_code'),
            provider_name=self.PROVIDER_NAME,
            # Only what has a clear operational purpose is kept - never the whole
            # raw payload (docs/phase2-sarvam-stt.md §Sarvam response mapping, §35).
            provider_metadata={'model': self.MODEL, 'request_id': raw.get('request_id')},
        )
