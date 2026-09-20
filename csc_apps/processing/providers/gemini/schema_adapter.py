"""Derives a Gemini-compatible JSON Schema from schemas/edar-schema.json
(docs/phase4-gemini-edar-extraction.md §eDAR schema integration). This is the ONE
place the eDAR field list is translated into Gemini's structured-output shape - it
reads and adapts the authoritative schema at runtime rather than hand-maintaining a
second, independently-editable field list (source instructions §77;
csc_apps/edar/tests.py has an alignment test confirming every field this adapter
emits still resolves via csc_apps.edar.schema_validation.resolve_field_key).
"""

# Device-captured (ADR-012) - never asked of Gemini, the only one of Module A's 6
# fields excluded from the response schema.
GPS_FIELD_KEY = 'gps_coordinates'

# Engineering safety bound, not a product/eDAR rule - the source spec places no cap
# on casualty count (unlike vehicles, capped at 3 by the schema itself). This only
# guards the structured-output request against a runaway/degenerate array.
MAX_CASUALTIES = 20

_FLAT_MODULE_IDS = ('A', 'B', 'C', 'F', 'G')

# eDAR data_type -> JSON Schema `type` (docs/domain-model.md, schemas/edar-schema.json
# field-level `data_type`). A list means a union of acceptable JSON types.
_DATA_TYPE_JSON_TYPE: dict[str, str | list[str]] = {
    'date': 'string',
    'time': 'string',
    'string': 'string',
    'categorical': 'string',
    'multi_label_categorical': 'array',
    'integer': 'integer',
    'integer_or_range': ['integer', 'string'],
    'categorical_or_boolean': ['string', 'boolean'],
    'boolean': 'boolean',
    'boolean_with_confidence': 'boolean',
    'ordinal': 'string',
    'categorical_or_text': 'string',
    'text_or_categorical': 'string',
    'free_text': 'string',
}


def _value_schema(field_def: dict) -> dict:
    data_type = field_def['data_type']
    base_types = _DATA_TYPE_JSON_TYPE[data_type]
    types = list(base_types) if isinstance(base_types, list) else [base_types]
    value_schema: dict = {'type': types + ['null']}

    has_resolved_enum = field_def.get('allowed_values_status') == 'resolved_from_source' and field_def.get(
        'allowed_values'
    )

    if data_type == 'multi_label_categorical':
        item_schema: dict = {'type': 'string'}
        if has_resolved_enum:
            item_schema['enum'] = list(field_def['allowed_values'])
        value_schema['items'] = item_schema
    elif has_resolved_enum:
        # Only constrain with a closed enum where the eDAR spec actually defines one
        # (docs/open-decisions.md - most categorical fields don't yet; those stay
        # free-text below rather than a fabricated enum - source instructions §17).
        value_schema['enum'] = list(field_def['allowed_values']) + [None]

    return value_schema


def _field_wrapper_schema(field_def: dict) -> dict:
    """Every extractable field is wrapped the same way: {value, confidence,
    evidence} - uniform regardless of data type, so persistence
    (csc_apps.processing.extraction_service) can treat every field identically."""
    return {
        'type': 'object',
        'properties': {
            'value': _value_schema(field_def),
            'confidence': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1},
            'evidence': {'type': ['string', 'null']},
        },
        'required': ['value', 'confidence', 'evidence'],
        'propertyOrdering': ['value', 'confidence', 'evidence'],
        # Confirmed supported by Gemini's response_json_schema (read from the
        # installed google-genai SDK's GenerateContentConfig.response_json_schema
        # docstring) - a schema-level backstop for "do not add fields that are not
        # in the schema" (source instructions §16), not just a prompt instruction.
        'additionalProperties': False,
    }


def _repeating_item_schema(module: dict) -> dict:
    properties, required, ordering = {}, [], []
    for field_def in module['fields']:
        key = field_def['field_key']
        properties[key] = _field_wrapper_schema(field_def)
        required.append(key)
        ordering.append(key)
    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'propertyOrdering': ordering,
        'additionalProperties': False,
    }


def build_response_schema(edar_schema: dict) -> dict:
    """The full Gemini `response_json_schema` for one extraction call - every Module
    A(minus GPS)/B/C/F/G field as a top-level wrapped property, plus `vehicles`
    (max 3, Module D) and `casualties` (max MAX_CASUALTIES, Module E) as arrays of
    wrapped-field objects."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}

    properties: dict = {}
    required: list[str] = []
    ordering: list[str] = []

    for module_id in _FLAT_MODULE_IDS:
        for field_def in modules_by_id[module_id]['fields']:
            key = field_def['field_key']
            if key == GPS_FIELD_KEY:
                continue
            properties[key] = _field_wrapper_schema(field_def)
            required.append(key)
            ordering.append(key)

    vehicle_module = modules_by_id['D']
    properties['vehicles'] = {
        'type': 'array',
        'maxItems': vehicle_module['max_repetitions'],
        'items': _repeating_item_schema(vehicle_module),
    }
    required.append('vehicles')
    ordering.append('vehicles')

    casualty_module = modules_by_id['E']
    properties['casualties'] = {
        'type': 'array',
        'maxItems': MAX_CASUALTIES,
        'items': _repeating_item_schema(casualty_module),
    }
    required.append('casualties')
    ordering.append('casualties')

    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'propertyOrdering': ordering,
        'additionalProperties': False,
    }


def flat_field_keys(edar_schema: dict) -> list[str]:
    """The 28 non-repeating field keys Gemini is actually asked to extract (Module A
    minus GPS, B, C, F, G) - used by csc_apps.processing.extraction_service to know
    the full expected key set when reconciling KNOWN vs UNKNOWN."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    keys = []
    for module_id in _FLAT_MODULE_IDS:
        for field_def in modules_by_id[module_id]['fields']:
            if field_def['field_key'] != GPS_FIELD_KEY:
                keys.append(field_def['field_key'])
    return keys


def repeating_field_keys(edar_schema: dict, entity: str) -> list[str]:
    """The per-instance field keys for `entity` ("vehicle" or "casualty")."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    module = next(m for m in modules_by_id.values() if m.get('repeat_entity') == entity)
    return [f['field_key'] for f in module['fields']]
