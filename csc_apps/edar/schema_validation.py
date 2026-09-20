"""Validates AI extraction results against schemas/edar-schema.json before any
EdarFieldValue row is written (docs/ai-extraction-contract.md §4, ADR-006/ADR-011).
"""

import datetime
import re

from csc_apps.edar.schema_loader import load_schema

_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


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
        raise ValueError(f'"{field_key}" is not a field in any non-repeatable eDAR module')

    if len(parts) == 3:
        entity, index_str, base_key = parts
        for module in schema['modules']:
            if not module.get('repeatable') or module.get('repeat_entity') != entity:
                continue
            if not index_str.isdigit() or int(index_str) < 1:
                raise ValueError(f'"{field_key}" has an invalid repetition index')
            index = int(index_str)
            max_repetitions = module.get('max_repetitions')
            if max_repetitions is not None and index > max_repetitions:
                raise ValueError(
                    f'"{field_key}" exceeds max_repetitions={max_repetitions} for "{entity}"'
                )
            for field in module['fields']:
                if field['field_key'] == base_key:
                    return field
            raise ValueError(f'"{base_key}" is not a field of the "{entity}" module')
        raise ValueError(f'"{entity}" is not a repeatable eDAR entity')

    raise ValueError(f'"{field_key}" is not a well-formed eDAR field key')


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
        raise ValueError(
            f'"{entry["field"]}": confidence and source must both be present or both absent '
            '(docs/ai-extraction-contract.md §3 - no confidence without evidence)'
        )

    if has_confidence:
        confidence = entry['confidence']
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            raise ValueError(f'"{entry["field"]}": confidence must be a float in [0, 1]')

        source = entry['source']
        start, end = source.get('start_time'), source.get('end_time')
        if start is not None and end is not None and start > end:
            raise ValueError(f'"{entry["field"]}": source.start_time is after source.end_time')


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

    if data_type == 'boolean' and not isinstance(value, bool):
        raise ValueError(f'"{field_key}" is boolean but got {value!r}')

    if data_type == 'integer' and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f'"{field_key}" is integer but got {value!r}')

    if data_type == 'date':
        if not isinstance(value, str):
            raise ValueError(f'"{field_key}" is date but got {value!r}')
        try:
            datetime.date.fromisoformat(value)
        except ValueError as e:
            raise ValueError(f'"{field_key}": not a valid ISO date (YYYY-MM-DD): {value!r}') from e

    if data_type == 'time':
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise ValueError(f'"{field_key}": not a valid HH:MM time: {value!r}')

    if data_type == 'multi_label_categorical' and not isinstance(value, list):
        raise ValueError(f'"{field_key}" is multi-label but got {value!r}')

    if field_def.get('allowed_values_status') == 'resolved_from_source' and field_def.get('allowed_values'):
        allowed = set(field_def['allowed_values'])
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if candidate not in allowed:
                raise ValueError(f'"{field_key}": {candidate!r} is not one of the allowed values {sorted(allowed)}')
