"""Google Gemini eDAR extraction, isolated behind ExtractionProvider
(docs/phase4-gemini-edar-extraction.md §Provider abstraction). No Gemini-specific
type or response shape leaves this module.

Verified against https://ai.google.dev (2026-09) and the installed `google-genai`
2.24.0 SDK source (not assumed from memory or an old tutorial - source instructions
§10/§12): the current recommended package is `google-genai` (not the deprecated
`google-generativeai`), the standard one-shot text-in/structured-JSON-out call is
`client.models.generate_content(model=, contents=, config=types.
GenerateContentConfig(response_mime_type='application/json', response_json_schema=))`,
and `response_json_schema` accepts a raw JSON Schema dict (confirmed by reading
`GenerateContentConfig`'s own docstring in `types.py`) - this is what
`csc_apps.processing.providers.gemini.schema_adapter` builds.

One extraction is three of these calls, not one: `response_json_schema` is checked
against a protobuf Schema with a hard total-complexity ceiling (live-verified
2026-09), and the single schema describing all 42 fields at once is rejected outright
(HTTP 400) once vehicles and casualties are added alongside the 28 flat fields,
independent of any individual field or keyword. `extract()` below calls Gemini once
each for the flat fields, vehicles, and casualties, then merges the three JSON
results before validation - the `ExtractionProvider.extract(english_text, schema) ->
ExtractionResult` interface itself is unchanged; the fan-out is entirely internal to
this provider.
"""

import json
import logging

from google import genai
from google.genai import errors, types

from csc.config import Configurations
from csc_apps.processing.providers.base import (
    ExtractedField,
    ExtractionProvider,
    ExtractionResult,
    FieldSource,
    ProviderError,
)
from csc_apps.processing.providers.gemini.prompt import (
    PROMPT_VERSION,
    TARGETED_PROMPT_VERSION,
    build_casualties_extraction_prompt,
    build_flat_extraction_prompt,
    build_targeted_extraction_prompt,
    build_vehicles_extraction_prompt,
)
from csc_apps.processing.providers.gemini.schema_adapter import (
    build_casualties_response_schema,
    build_flat_response_schema,
    build_targeted_response_schema,
    build_vehicles_response_schema,
    flat_field_keys,
    repeating_field_keys,
)

logger = logging.getLogger(__name__)

_STATUS_CODE_TO_ERROR_CODE = {
    400: 'EXTRACTION_UNSUPPORTED_INPUT',
    401: 'EXTRACTION_AUTHENTICATION_FAILED',
    403: 'EXTRACTION_AUTHENTICATION_FAILED',
    404: 'EXTRACTION_UNSUPPORTED_INPUT',
    429: 'EXTRACTION_PROVIDER_RATE_LIMITED',
    500: 'EXTRACTION_PROVIDER_UNAVAILABLE',
    503: 'EXTRACTION_PROVIDER_UNAVAILABLE',
}


def _classify_api_error(error: errors.APIError) -> str:
    return _STATUS_CODE_TO_ERROR_CODE.get(error.code, 'EXTRACTION_PROVIDER_UNAVAILABLE')


class GeminiExtractionProvider(ExtractionProvider):
    PROVIDER_NAME = 'gemini'
    # Verified 2026-09 against ai.google.dev/gemini-api/docs/models: stable/GA
    # ("most intelligent Flash model", not a "-preview" label), supports structured
    # JSON output. Chosen over gemini-3.1-pro-preview specifically because "preview"
    # signals non-GA support status - a meaningful risk for a system feeding
    # legal/insurance-relevant crash reports (docs/phase4-gemini-edar-extraction.md
    # §Gemini model, ADR-018).
    MODEL = 'gemini-3.8-flash'

    def __init__(self):
        self._api_key = Configurations.gemini_api_key
        self._http_timeout_seconds = Configurations.gemini_http_timeout_seconds

    def _client(self) -> genai.Client:
        if not self._api_key:
            raise ProviderError('EXTRACTION_AUTHENTICATION_FAILED', 'GEMINI_API_KEY is not configured')
        return genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=self._http_timeout_seconds * 1000),
        )

    def extract(
        self, english_text: str, schema: dict, target_fields: list[str] | None = None,
        entity_context_lines: list[str] | None = None,
    ) -> ExtractionResult:
        """Three separate Gemini calls - flat crash-level fields, vehicles,
        casualties - merged into one candidate before validation
        (docs/phase4-gemini-edar-extraction.md §Structured-output strategy). Not
        three independent extractions: any one call failing fails the whole
        extraction (no partial AI eDAR is ever built from two calls' worth of
        fields), same "all or nothing" guarantee the single-call version gave.

        `target_fields` (Phase 10B, docs/phase10b-supplemental-audio.md §Targeted
        extraction): when given, this is a supplemental-audio call instead - ONE
        call, restricted to exactly this field-key set, using
        build_targeted_response_schema/build_targeted_extraction_prompt rather than
        the fixed three-call split. `entity_context_lines` (only meaningful
        alongside `target_fields`) is passed straight through to the targeted
        prompt builder to help Gemini match new information to an already-
        identified vehicle/casualty index."""
        if not english_text or not english_text.strip():
            raise ProviderError('EXTRACTION_UNSUPPORTED_INPUT', 'English transcript is empty')

        client = self._client()

        if target_fields is not None:
            return self._extract_targeted(client, english_text, schema, target_fields, entity_context_lines or [])

        flat_candidate = self._call(
            client, build_flat_extraction_prompt(english_text, schema), build_flat_response_schema(schema)
        )
        vehicles_candidate = self._call(
            client, build_vehicles_extraction_prompt(english_text, schema), build_vehicles_response_schema(schema)
        )
        casualties_candidate = self._call(
            client, build_casualties_extraction_prompt(english_text, schema), build_casualties_response_schema(schema)
        )

        candidate = {
            **flat_candidate,
            'vehicles': vehicles_candidate.get('vehicles'),
            'casualties': casualties_candidate.get('casualties'),
        }
        fields, vehicle_count, casualty_count = self._flatten_candidate(candidate, schema)

        return ExtractionResult(
            fields=fields,
            provider_name=self.PROVIDER_NAME,
            extraction_version=(
                f'gemini-{self.MODEL}/prompt-{PROMPT_VERSION}/schema-{schema.get("schema_version")}'
            ),
            provider_metadata={
                'model': self.MODEL,
                'prompt_version': PROMPT_VERSION,
                'schema_version': schema.get('schema_version'),
                'vehicle_count': vehicle_count,
                'casualty_count': casualty_count,
                'call_count': 3,
            },
        )

    def _extract_targeted(
        self, client: genai.Client, english_text: str, schema: dict, target_fields: list[str],
        entity_context_lines: list[str],
    ) -> ExtractionResult:
        prompt = build_targeted_extraction_prompt(english_text, schema, target_fields, entity_context_lines)
        response_schema = build_targeted_response_schema(schema, target_fields)
        candidate = self._call(client, prompt, response_schema)
        fields = self._flatten_targeted_candidate(candidate, target_fields)

        return ExtractionResult(
            fields=fields,
            provider_name=self.PROVIDER_NAME,
            extraction_version=(
                f'gemini-{self.MODEL}/prompt-{TARGETED_PROMPT_VERSION}/schema-{schema.get("schema_version")}'
            ),
            provider_metadata={
                'model': self.MODEL,
                'prompt_version': TARGETED_PROMPT_VERSION,
                'schema_version': schema.get('schema_version'),
                'call_count': 1,
                'target_field_count': len(target_fields),
            },
        )

    @staticmethod
    def _flatten_targeted_candidate(candidate: dict, target_fields: list[str]) -> list[ExtractedField]:
        """Same {value, confidence, evidence} unwrapping as _flatten_candidate, but
        driven entirely by `target_fields` rather than the full schema - a targeted
        response only ever contains the flat keys, and the vehicles/casualties
        arrays, that build_targeted_response_schema actually asked for."""
        fields: list[ExtractedField] = []
        flat_keys = [k for k in target_fields if '.' not in k]
        for key in flat_keys:
            _append_if_known(fields, key, candidate.get(key))

        for entity, response_key in (('vehicle', 'vehicles'), ('casualty', 'casualties')):
            wanted: dict[int, set[str]] = {}
            for key in target_fields:
                parts = key.split('.')
                if len(parts) == 3 and parts[0] == entity:
                    wanted.setdefault(int(parts[1]), set()).add(parts[2])
            if not wanted:
                continue
            items = candidate.get(response_key) or []
            for index, item in enumerate(items, start=1):
                if index not in wanted or not isinstance(item, dict):
                    continue
                for base_key in wanted[index]:
                    _append_if_known(fields, f'{entity}.{index}.{base_key}', item.get(base_key))

        return fields

    def _call(self, client: genai.Client, prompt: str, response_schema: dict) -> dict:
        """Runs one of the three extraction calls and returns its parsed JSON
        object. Errors are classified identically regardless of which of the three
        calls raised them - the caller (extraction_service) only ever sees one
        extraction attempt, not three independently-retryable ones."""
        try:
            response = client.models.generate_content(
                model=self.MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_json_schema=response_schema,
                    # Deterministic-leaning: this is information extraction, not
                    # creative generation - we want the most literal reading of the
                    # transcript the model can produce, not varied phrasing.
                    temperature=0,
                ),
            )
        except errors.APIError as e:
            raise ProviderError(_classify_api_error(e), f'Gemini API error ({e.code}): {e.message}') from e
        except TimeoutError as e:
            raise ProviderError('EXTRACTION_PROVIDER_TIMEOUT', str(e)) from e

        raw_text = response.text
        if not raw_text or not raw_text.strip():
            raise ProviderError('EXTRACTION_EMPTY_OUTPUT', 'Gemini returned an empty response')

        try:
            candidate = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise ProviderError('EXTRACTION_MALFORMED_RESPONSE', f'Gemini output was not valid JSON: {e}') from e

        if not isinstance(candidate, dict):
            raise ProviderError(
                'EXTRACTION_MALFORMED_RESPONSE', f'Gemini output was not a JSON object: {type(candidate)}'
            )

        return candidate

    @staticmethod
    def _flatten_candidate(candidate: dict, schema: dict) -> tuple[list[ExtractedField], int, int]:
        """Converts Gemini's nested {value, confidence, evidence}-wrapped JSON object
        into the flat ExtractedField list the ExtractionProvider interface expects -
        only for fields where `value` is non-null (a field with no evidence is
        omitted, per ExtractedField's documented contract, not represented with a
        null value)."""
        fields: list[ExtractedField] = []

        for key in flat_field_keys(schema):
            _append_if_known(fields, key, candidate.get(key))

        vehicles = candidate.get('vehicles') or []
        for index, vehicle in enumerate(vehicles, start=1):
            if not isinstance(vehicle, dict):
                continue
            for base_key in repeating_field_keys(schema, 'vehicle'):
                _append_if_known(fields, f'vehicle.{index}.{base_key}', vehicle.get(base_key))

        casualties = candidate.get('casualties') or []
        for index, casualty in enumerate(casualties, start=1):
            if not isinstance(casualty, dict):
                continue
            for base_key in repeating_field_keys(schema, 'casualty'):
                _append_if_known(fields, f'casualty.{index}.{base_key}', casualty.get(base_key))

        return fields, len(vehicles), len(casualties)


def _append_if_known(fields: list[ExtractedField], field_key: str, entry) -> None:
    if not isinstance(entry, dict) or entry.get('value') is None:
        return
    # Passed through exactly as Gemini provided it, including a malformed
    # confidence/evidence pairing if the model violated the prompt's own "null iff
    # value is null" instruction - csc_apps.edar.schema_validation.
    # validate_extraction_entry (untrusted-provider-output validation, source
    # instructions §27-28) is what actually rejects that, not this method silently
    # repairing it.
    fields.append(
        ExtractedField(
            field=field_key,
            value=entry['value'],
            confidence=entry.get('confidence'),
            source=FieldSource(transcript_segment=entry.get('evidence') or ''),
        )
    )
