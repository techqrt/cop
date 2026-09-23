"""Validates AI extraction results against schemas/edar-schema.json before any
EdarFieldValue row is written (docs/ai-extraction-contract.md §4, ADR-006/ADR-011).
"""

import datetime
import re

from csc_apps.edar.schema_loader import load_schema

_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


class EdarValidationError(ValueError):
    """A deterministic eDAR validation failure carrying a machine-readable `code`
    (docs/phase5-validation-provenance.md §Validation layers) so
    csc_apps.edar.quality_validation can build a structured report without parsing
    message text. Subclasses ValueError so every pre-Phase-5 caller/test that catches
    ValueError keeps working unchanged. `str(error)` may contain the offending value
    and is for internal use only - never exposed through the API."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def resolve_field_key(field_key: str, schema: dict | None = None) -> dict:
    """Returns the schema field definition for `field_key`, which is either a bare
    module A/B/C/F/G field ("crash_date") or a repeating-group entry
    ("vehicle.1.vehicle_type", "casualty.2.injury_severity" - docs/domain-model.md §2).
    Raises ValueError if the key doesn't resolve to a real, in-bounds schema field.
    """
    schema = schema or load_schema()
    parts = field_key.split('.')

    if len(parts) == 1:
        base_key = parts[0]
        for module in schema['modules']:
            if module['repeatable']:
                continue
            for field in module['fields']:
                if field['field_key'] == base_key:
                    return field
        raise EdarValidationError('invalid_field_key', f'"{field_key}" is not a field in any non-repeatable eDAR module')

    if len(parts) == 3:
        entity, index_str, base_key = parts
        for module in schema['modules']:
            if not module.get('repeatable') or module.get('repeat_entity') != entity:
                continue
            if not index_str.isdigit() or int(index_str) < 1:
                raise EdarValidationError('invalid_field_key', f'"{field_key}" has an invalid repetition index')
            index = int(index_str)
            max_repetitions = module.get('max_repetitions')
            if max_repetitions is not None and index > max_repetitions:
                raise EdarValidationError(
                    'entity_limit_exceeded',
                    f'"{field_key}" exceeds max_repetitions={max_repetitions} for "{entity}"',
                )
            for field in module['fields']:
                if field['field_key'] == base_key:
                    return field
            raise EdarValidationError('invalid_field_key', f'"{base_key}" is not a field of the "{entity}" module')
        raise EdarValidationError('invalid_field_key', f'"{entity}" is not a repeatable eDAR entity')

    raise EdarValidationError('invalid_field_key', f'"{field_key}" is not a well-formed eDAR field key')


def validate_extraction_entry(entry: dict, schema: dict | None = None) -> None:
    """entry is one result item in the shape from docs/ai-extraction-contract.md §2:
    {"field", "value", "confidence", "source": {"transcript_segment", "start_time",
    "end_time"}}. `source.start_time`/`end_time` are optional (Phase 4's Gemini-based
    extraction has no audio timestamps to offer - only a text `transcript_segment` -
    docs/phase4-gemini-edar-extraction.md §Provenance); when present, they're still
    checked for internal consistency. Raises ValueError describing the first
    violation found."""
    schema = schema or load_schema()
    field_def = resolve_field_key(entry['field'], schema=schema)
    value = entry.get('value')

    if value is not None:
        _validate_value_type(entry['field'], value, field_def)

    has_confidence = entry.get('confidence') is not None
    has_source = entry.get('source') is not None
    if has_confidence != has_source:
        raise EdarValidationError(
            'provenance_incomplete',
            f'"{entry["field"]}": confidence and source must both be present or both absent '
            '(docs/ai-extraction-contract.md §3 - no confidence without evidence)',
        )

    if has_confidence:
        confidence = entry['confidence']
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            raise EdarValidationError('invalid_confidence', f'"{entry["field"]}": confidence must be a float in [0, 1]')

        source = entry['source']
        start, end = source.get('start_time'), source.get('end_time')
        if start is not None and end is not None and start > end:
            raise EdarValidationError(
                'invalid_evidence_timing', f'"{entry["field"]}": source.start_time is after source.end_time'
            )


_KNOWN_STATES = ('KNOWN', 'UNKNOWN', 'NOT_APPLICABLE', 'UNCERTAIN')


def validate_approved_value(field_key: str, known: str, value, schema: dict | None = None) -> None:
    """Validates one officer-submitted field edit for Phase 6 approval
    (docs/phase6-officer-review-approval.md §Validation). Reuses the same
    structural/type checks Phase 4/5 apply to AI output (`_validate_value_type`) -
    officer input gets no separate, second-guessed validation engine. Unlike an AI
    entry, there is no confidence/evidence to cross-check: a human reviewer is
    asserting the value directly, not offering a model-generated signal.
    `known` follows the existing EdarFieldValue.KNOWN_CHOICES states
    (docs/unknown-data-policy.md) - unchanged from what the AI layer already uses, no
    new convention. A non-KNOWN state must carry a null value, same as every AI
    UNKNOWN row already does."""
    schema = schema or load_schema()
    field_def = resolve_field_key(field_key, schema=schema)

    if known not in _KNOWN_STATES:
        raise EdarValidationError('invalid_known_state', f'"{field_key}": {known!r} is not a valid known state')

    if known == 'KNOWN':
        if value is None:
            raise EdarValidationError('missing_value', f'"{field_key}": known=KNOWN requires a non-null value')
        _validate_value_type(field_key, value, field_def)
    elif value is not None:
        raise EdarValidationError('value_not_allowed', f'"{field_key}": known={known} must have a null value')


def _validate_value_type(field_key: str, value, field_def: dict) -> None:
    """Layer 1 (structural/type) validation for a non-null value
    (docs/phase4-gemini-edar-extraction.md §Schema validation, source instructions
    §29). Deliberately does not check categorical values against an enum here for
    most fields - schemas/edar-schema.json leaves most categorical enums
    unresolved (docs/open-decisions.md), so there is nothing authoritative to check
    against yet; only `injury_severity` currently has `allowed_values_status:
    "resolved_from_source"`, checked below like any other field with real allowed
    values."""
    data_type = field_def['data_type']

    # Plain string-shaped types (docs/domain-model.md - the JSON type Gemini's own
    # response_json_schema already enforces for AI output, per
    # csc_apps.processing.providers.gemini.schema_adapter's _DATA_TYPE_JSON_TYPE
    # mapping). AI values never needed this check to actually reject anything -
    # Gemini's request-time schema already constrained the type before this ever
    # ran. Officer-submitted values (Phase 6) have no such upstream gate; DRF's
    # JSONField happily accepts `123` for a string field, so this check is what
    # actually rejects it (found via a Phase 6 acceptance test, not a hypothetical).
    if data_type in ('string', 'categorical', 'ordinal', 'categorical_or_text', 'text_or_categorical', 'free_text'):
        if not isinstance(value, str):
            raise EdarValidationError('invalid_type', f'"{field_key}" is {data_type} but got {value!r}')

    if data_type == 'boolean' and not isinstance(value, bool):
        raise EdarValidationError('invalid_type', f'"{field_key}" is boolean but got {value!r}')

    if data_type == 'integer' and (isinstance(value, bool) or not isinstance(value, int)):
        raise EdarValidationError('invalid_type', f'"{field_key}" is integer but got {value!r}')

    if data_type == 'date':
        if not isinstance(value, str):
            raise EdarValidationError('invalid_type', f'"{field_key}" is date but got {value!r}')
        try:
            datetime.date.fromisoformat(value)
        except ValueError as e:
            raise EdarValidationError(
                'invalid_type', f'"{field_key}": not a valid ISO date (YYYY-MM-DD): {value!r}'
            ) from e

    if data_type == 'time':
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise EdarValidationError('invalid_type', f'"{field_key}": not a valid HH:MM time: {value!r}')

    if data_type == 'multi_label_categorical' and not isinstance(value, list):
        raise EdarValidationError('invalid_type', f'"{field_key}" is multi-label but got {value!r}')

    if field_def.get('allowed_values_status') == 'resolved_from_source' and field_def.get('allowed_values'):
        allowed = set(field_def['allowed_values'])
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if candidate not in allowed:
                raise EdarValidationError(
                    'invalid_enum', f'"{field_key}": {candidate!r} is not one of the allowed values {sorted(allowed)}'
                )
