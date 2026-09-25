"""Provider-agnostic interfaces for the three AI pipeline stages
(docs/processing-pipeline.md §4, docs/ai-extraction-contract.md). No concrete
provider is implemented in Phase 0 (docs/product-scope.md §4, docs/open-decisions.md
OD-002) - these are contracts a Phase 1 provider package implements, so no
provider-specific SDK or request/response shape ever appears in the business layer.
"""

import abc
import dataclasses


class ProviderError(Exception):
    """Raised by any provider implementation on failure. `error_code` is looked up in
    csc_apps.processing.error_classification by the calling service to decide
    retryable vs. non-retryable (docs/error-retry-strategy.md,
    docs/phase2-sarvam-stt.md §Error handling) - the provider itself never decides
    that, it only classifies *what kind* of failure occurred."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)


@dataclasses.dataclass
class TranscriptResult:
    text: str
    detected_language_code: str | None
    provider_name: str
    provider_metadata: dict


@dataclasses.dataclass
class TranslationResult:
    text: str
    source_language_code: str
    target_language_code: str
    provider_name: str
    provider_metadata: dict


@dataclasses.dataclass
class FieldSource:
    transcript_segment: str
    # Optional (changed in Phase 4 from Phase 0's placeholder `float`) - completing
    # the interface: text-based extraction (docs/phase4-gemini-edar-extraction.md)
    # has no audio timestamps to offer (both Sarvam calls in this pipeline run with
    # with_timestamps=False), only a supporting transcript excerpt. Left as `float`
    # for a future provider (e.g. one extracting directly from timestamped audio)
    # that can supply real values.
    start_time: float | None = None
    end_time: float | None = None


@dataclasses.dataclass
class ExtractedField:
    """One entry of an extraction result (docs/ai-extraction-contract.md §2). A field
    the provider found no evidence for is simply absent from the result list - never
    represented as an ExtractedField with a null/guessed value."""

    field: str
    value: object
    confidence: float
    source: FieldSource


@dataclasses.dataclass
class ExtractionResult:
    fields: list[ExtractedField]
    provider_name: str
    extraction_version: str
    provider_metadata: dict


class SpeechToTextProvider(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, local_audio_path: str, content_type: str) -> TranscriptResult:
        """`local_audio_path` is a path to a local, already-retrieved copy of the
        audio - the caller (csc_apps.processing.stt_service) is responsible for
        pulling it out of AudioStorage into secure temporary storage first and
        deleting it afterwards (docs/phase2-sarvam-stt.md §Audio retrieval,
        §Immutable source audio). This method must never write to, delete, or treat
        this path as canonical storage. Raises ProviderError on failure; the caller
        classifies it via csc_apps.processing.error_classification, not this method.

        Changed in Phase 2 (was `audio_storage_path: str`, the opaque storage key) -
        completing what Phase 0 left as a placeholder interface: a provider must not
        be responsible for storage-path discovery (docs/phase2-sarvam-stt.md §11).
        """
        raise NotImplementedError


class TranslationProvider(abc.ABC):
    @abc.abstractmethod
    def translate(self, text: str, source_language_code: str, target_language_code: str) -> TranslationResult:
        """`source_language_code` is required, not optional (changed in Phase 3 from
        Phase 0's placeholder `str | None`) - the caller
        (csc_apps.processing.translation_service) must resolve it from the existing
        original Transcript before calling this, never leave it to the provider to
        guess or auto-detect (docs/phase3-sarvam-translation.md §Source language).
        Raises ProviderError on failure; the caller classifies it via
        csc_apps.processing.error_classification, not this method."""
        raise NotImplementedError


class ExtractionProvider(abc.ABC):
    @abc.abstractmethod
    def extract(
        self, english_text: str, schema: dict, target_fields: list[str] | None = None
    ) -> ExtractionResult:
        """`schema` is the parsed contents of schemas/edar-schema.json
        (docs/ai-extraction-contract.md §1). `english_text` is the persisted
        Transcript(language=ENGLISH) text - never the original-language transcript
        or raw audio (docs/phase4-gemini-edar-extraction.md §Input).

        `ExtractionResult.fields` includes an entry only for fields the provider
        found evidence for - a field with no supporting evidence is omitted, never
        included with a null/guessed value (see ExtractedField's docstring). The
        caller (csc_apps.processing.extraction_service) is responsible for
        reconciling this against the full expected field-key set and persisting the
        rest as `known=UNKNOWN` (docs/unknown-data-policy.md) - that reconciliation
        is a schema/persistence concern, not something a provider should decide.

        `target_fields` (Phase 10B, docs/phase10b-supplemental-audio.md
        §Targeted extraction): when given, restricts extraction to exactly this
        field-key subset (used for supplemental-audio processing, where the caller
        has already determined which fields are still unresolved) - `None` (the
        default) means "extract every field this provider normally asks about",
        unchanged from before Phase 10B. A provider implementation is free to
        return fields outside `target_fields` (untrusted output) - the caller
        enforces the restriction, not the provider.

        Raises ProviderError on failure; the caller classifies it via
        csc_apps.processing.error_classification, not this method."""
        raise NotImplementedError
