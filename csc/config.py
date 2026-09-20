from decouple import config


class Configurations:
    """Server-side configuration, sourced from environment variables only.

    Ported from pms/config.py (docs/pms-reference-analysis.md §1) - never hold a
    secret's literal value here, only the decouple.config() lookup.
    """

    db_name = config('DB_NAME')
    db_user = config('DB_USER')
    db_password = config('DB_PASSWORD')
    db_host = config('DB_HOST')
    db_port = config('DB_PORT')
    pagination_count = 10
    max_pagination_limit = 200
    debug = False if config('DEBUG', default='False') == 'False' else True

    access_token_lifetime_days = config('ACCESS_TOKEN_LIFETIME_DAYS', default=7, cast=int)

    # Phase 1 audio ingestion (docs/phase1-audio-ingestion.md, docs/open-decisions.md
    # OD-010) - interim defaults, not sourced from a product requirement. 100MB covers
    # an uncompressed WAV recording well beyond the eDAR brief's "within 10 minutes at
    # the scene" scoping note; revisit once real device/provider data exists.
    audio_max_upload_size_bytes = config('AUDIO_MAX_UPLOAD_SIZE_BYTES', default=100 * 1024 * 1024, cast=int)
    private_storage_root = config('PRIVATE_STORAGE_ROOT', default='private_storage')

    # Phase 2 - Sarvam Speech-to-Text (docs/phase2-sarvam-stt.md §Configuration).
    # Backend-only credential; never returned by any API, never logged
    # (csc_apps/processing/providers/sarvam/provider.py never logs it).
    sarvam_api_key = config('SARVAM_API_KEY', default='')
    # Per-HTTP-request timeout passed straight to the Sarvam SDK client
    # (docs/phase2-sarvam-stt.md §Provider timeouts) - never an unbounded request.
    sarvam_http_timeout_seconds = config('SARVAM_HTTP_TIMEOUT_SECONDS', default=60, cast=int)
    # Batch job polling: how often to check status, and the overall ceiling before
    # giving up and classifying the job as a (retryable) timeout.
    sarvam_poll_interval_seconds = config('SARVAM_POLL_INTERVAL_SECONDS', default=5, cast=int)
    sarvam_poll_timeout_seconds = config('SARVAM_POLL_TIMEOUT_SECONDS', default=600, cast=int)

    # Phase 4 - Gemini eDAR extraction (docs/phase4-gemini-edar-extraction.md
    # §Configuration). Backend-only credential; never returned by any API, never
    # logged (csc_apps/processing/providers/gemini/provider.py never logs it). Model
    # name is a provider constant, not configured here, matching how Phase 2/3
    # hardcode their fixed model choices - see GeminiExtractionProvider.MODEL.
    gemini_api_key = config('GEMINI_API_KEY', default='')
    gemini_http_timeout_seconds = config('GEMINI_HTTP_TIMEOUT_SECONDS', default=120, cast=int)
