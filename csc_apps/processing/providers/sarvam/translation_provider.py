"""Sarvam Text Translation integration, isolated behind TranslationProvider
(docs/phase3-sarvam-translation.md §Provider abstraction). No Sarvam-specific type or
response shape leaves this module.

Verified against https://docs.sarvam.ai (2026-09): `POST /translate`, model
`sarvam-translate:v1` (formal-style, all 22 scheduled Indian languages + English,
2,000-character input limit per request - `sarvam-translate:v1` does **not** support
`source_language_code="auto"`, unlike `mayura:v1` - docs/phase3-sarvam-translation.md
§Source language). Uses the official `sarvamai` SDK's `client.text.translate(...)`,
the same package Phase 2's STT provider already depends on
(`requirements.txt` - no new dependency).

This provider builds its own SarvamAI client independently of
csc_apps.processing.providers.sarvam.provider (the Phase 2 STT provider) rather than
sharing a client-construction helper - a deliberate choice to guarantee the Phase 2
file is completely untouched by Phase 3, at the cost of a few duplicated lines.
"""

import httpx
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

from csc.config import Configurations
from csc_apps.processing.providers.base import ProviderError, TranslationProvider, TranslationResult
from csc_apps.processing.providers.sarvam.chunker import chunk_text

_STATUS_CODE_TO_ERROR_CODE = {
    400: 'TRANSLATION_UNSUPPORTED_INPUT',
    403: 'TRANSLATION_AUTHENTICATION_FAILED',
    422: 'TRANSLATION_UNSUPPORTED_INPUT',
    429: 'TRANSLATION_PROVIDER_RATE_LIMITED',
    500: 'TRANSLATION_PROVIDER_UNAVAILABLE',
    503: 'TRANSLATION_PROVIDER_UNAVAILABLE',
}

# sarvam-translate:v1's source_language_code options, per the SDK's own
# TranslateSourceLanguage type - deliberately excludes "auto", which the Sarvam docs
# state only mayura:v1 supports. A Transcript whose detected_language_code is None,
# empty, or "unknown" (Sarvam STT's own auto-detect-failed marker) cannot be
# translated by this model - the caller must not guess a language for it
# (docs/phase3-sarvam-translation.md §Source language).
SUPPORTED_SOURCE_LANGUAGE_CODES = frozenset({
    'bn-IN', 'en-IN', 'gu-IN', 'hi-IN', 'kn-IN', 'ml-IN', 'mr-IN', 'od-IN', 'pa-IN', 'ta-IN', 'te-IN',
    'as-IN', 'brx-IN', 'doi-IN', 'kok-IN', 'ks-IN', 'mai-IN', 'mni-IN', 'ne-IN', 'sa-IN', 'sat-IN',
    'sd-IN', 'ur-IN',
})


def _classify_api_error(error: ApiError) -> str:
    return _STATUS_CODE_TO_ERROR_CODE.get(error.status_code, 'TRANSLATION_PROVIDER_UNAVAILABLE')


class SarvamTranslationProvider(TranslationProvider):
    PROVIDER_NAME = 'sarvam'
    # Fixed by product decision (docs/architecture-decisions.md ADR-017) - formal-
    # style translation, appropriate for official/professional reporting. Never
    # silently switched to mayura:v1.
    MODEL = 'sarvam-translate:v1'
    MODE = 'formal'

    def __init__(self):
        self._api_key = Configurations.sarvam_api_key
        self._http_timeout_seconds = Configurations.sarvam_http_timeout_seconds

    def _client(self) -> SarvamAI:
        if not self._api_key:
            raise ProviderError('TRANSLATION_AUTHENTICATION_FAILED', 'SARVAM_API_KEY is not configured')
        return SarvamAI(api_subscription_key=self._api_key, timeout=self._http_timeout_seconds)

    def translate(self, text: str, source_language_code: str, target_language_code: str) -> TranslationResult:
        if source_language_code not in SUPPORTED_SOURCE_LANGUAGE_CODES:
            raise ProviderError(
                'TRANSLATION_UNSUPPORTED_INPUT',
                f'"{source_language_code}" is not a source language sarvam-translate:v1 supports '
                '(it does not support "auto" detection - the caller must supply a real code)',
            )

        chunks = chunk_text(text)
        if not chunks:
            raise ProviderError('TRANSLATION_EMPTY_OUTPUT', 'No text to translate')

        client = self._client()
        # Sequential, in source order - trivially preserves chunk order and keeps
        # request volume predictable (no uncontrolled fan-out against Sarvam's rate
        # limits). A chunk failing partway raises immediately, so translate() never
        # returns a partially-assembled result - the caller
        # (csc_apps.processing.translation_service) treats the whole attempt as
        # failed, never persisting a canonical partial transcript
        # (docs/phase3-sarvam-translation.md §Partial translation failure).
        translated_chunks = [
            self._translate_one_chunk(client, chunk, source_language_code, target_language_code, index, len(chunks))
            for index, chunk in enumerate(chunks)
        ]

        combined_text = ''.join(translated_chunks)
        if not combined_text.strip():
            raise ProviderError('TRANSLATION_EMPTY_OUTPUT', 'Sarvam returned an empty translation for this text')

        return TranslationResult(
            text=combined_text,
            source_language_code=source_language_code,
            target_language_code=target_language_code,
            provider_name=self.PROVIDER_NAME,
            # Only what has a clear operational purpose - never the full raw
            # response per chunk (docs/phase3-sarvam-translation.md §Provider
            # metadata).
            provider_metadata={'model': self.MODEL, 'chunk_count': len(chunks)},
        )

    def _translate_one_chunk(
        self, client: SarvamAI, chunk: str, source_language_code: str, target_language_code: str, index: int, total: int
    ) -> str:
        try:
            response = client.text.translate(
                input=chunk,
                source_language_code=source_language_code,
                target_language_code=target_language_code,
                model=self.MODEL,
                mode=self.MODE,
            )
        except ApiError as e:
            raise ProviderError(
                _classify_api_error(e), f'Sarvam translation error (chunk {index + 1}/{total}): {e.body}'
            ) from e
        except httpx.TimeoutException as e:
            raise ProviderError('TRANSLATION_PROVIDER_TIMEOUT', str(e)) from e
        except httpx.TransportError as e:
            raise ProviderError('TRANSLATION_PROVIDER_UNAVAILABLE', str(e)) from e

        if not isinstance(response.translated_text, str):
            raise ProviderError(
                'TRANSLATION_MALFORMED_RESPONSE',
                f'"translated_text" missing or not a string (chunk {index + 1}/{total}): '
                f'{type(response.translated_text)}',
            )
        return response.translated_text
