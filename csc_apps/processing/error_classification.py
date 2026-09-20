"""Retryable/non-retryable failure classification (docs/error-retry-strategy.md §1).
A single lookup, not scattered per-provider try/except logic - same "funnel through
one place" instinct as csc_apps.common.common.Common.exception_handler
(docs/pms-reference-analysis.md §8), applied to the pipeline instead of the HTTP layer.
"""

RETRYABLE_ERROR_CODES = {
    'AUDIO_STORAGE_IO_ERROR',
    'STT_PROVIDER_TIMEOUT',
    'STT_PROVIDER_UNAVAILABLE',
    'STT_PROVIDER_RATE_LIMITED',  # Phase 2 - Sarvam 429
    'TRANSLATION_PROVIDER_TIMEOUT',
    'TRANSLATION_PROVIDER_UNAVAILABLE',
    'TRANSLATION_PROVIDER_RATE_LIMITED',  # Phase 3 - Sarvam 429
    'EXTRACTION_PROVIDER_TIMEOUT',
    'EXTRACTION_PROVIDER_UNAVAILABLE',
    'EXTRACTION_PROVIDER_RATE_LIMITED',  # Phase 4 - Gemini 429
    'DATABASE_WRITE_ERROR',
}

NON_RETRYABLE_ERROR_CODES = {
    'INVALID_AUDIO',
    'STT_UNSUPPORTED_INPUT',
    'STT_AUTHENTICATION_FAILED',  # Phase 2 - Sarvam 403 / missing SARVAM_API_KEY
    'STT_MALFORMED_RESPONSE',  # Phase 2 - unparseable/malformed Sarvam output
    'STT_EMPTY_TRANSCRIPT',  # Phase 2 - a valid but empty transcript; retrying the
    # same audio through the same provider is expected to reproduce it
    'TRANSLATION_EMPTY_OUTPUT',
    'TRANSLATION_UNSUPPORTED_INPUT',  # Phase 3 - Sarvam 400/422, or a source language
    # the original Transcript doesn't have / sarvam-translate:v1 doesn't support
    'TRANSLATION_AUTHENTICATION_FAILED',  # Phase 3 - Sarvam 403 / missing SARVAM_API_KEY
    'TRANSLATION_MALFORMED_RESPONSE',  # Phase 3 - unparseable/malformed Sarvam output
    'EXTRACTION_MALFORMED_RESPONSE',
    'EXTRACTION_SCHEMA_VALIDATION_FAILED',
    'EXTRACTION_AUTHENTICATION_FAILED',  # Phase 4 - Gemini 401/403 / missing GEMINI_API_KEY
    'EXTRACTION_UNSUPPORTED_INPUT',  # Phase 4 - Gemini 400/404, or an empty English transcript
    'EXTRACTION_EMPTY_OUTPUT',  # Phase 4 - Gemini returned no response body
}


def is_retryable(error_code: str) -> bool:
    """Raises ValueError for an unrecognized error_code rather than silently guessing
    retryable/non-retryable - an unclassified failure mode should fail loudly during
    development, not default to either behavior."""
    if error_code in RETRYABLE_ERROR_CODES:
        return True
    if error_code in NON_RETRYABLE_ERROR_CODES:
        return False
    raise ValueError(f'Unclassified error_code: {error_code}')
