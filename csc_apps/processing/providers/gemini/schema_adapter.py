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
# on casualty count (unlike vehicles, capped at 3 by the schema itself). Originally
# just a guard against a runaway/degenerate array; now load-bearing for a second
# reason too - Gemini's response_json_schema is checked against a protobuf Schema
# with a hard total-complexity ceiling, and the casualties call's own schema (6
# fields x {value,confidence,evidence} each, times maxItems) hits it on its own,
# independent of the flat-fields and vehicles calls. Live-verified 2026-09 against
# the real API with this project's actual casualty field set: maxItems=8 is accepted,
# maxItems=9 is rejected outright (HTTP 400). 6 keeps a small margin below that
# observed edge rather than sitting exactly on it.
MAX_CASUALTIES = 6

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
    # Gemini's response_json_schema is validated against a protobuf Schema whose
    # `type` field holds exactly one value, not a repeating list (confirmed against
    # the installed google-genai SDK's Schema type, which pairs a single `type` with
    # a separate `nullable: bool`; live-verified 2026-09 - a schema.json-style
    # `"type": [X, "null"]` union is rejected outright with HTTP 400 "Request
    # contains an invalid argument", not accepted-but-ignored). A true multi-type
    # union (integer_or_range, categorical_or_boolean) is expressed as `anyOf` with
    # one single-type branch per alternative instead.
    data_type = field_def['data_type']
    base_types = _DATA_TYPE_JSON_TYPE[data_type]
    types = list(base_types) if isinstance(base_types, list) else [base_types]

    has_resolved_enum = field_def.get('allowed_values_status') == 'resolved_from_source' and field_def.get(
        'allowed_values'
    )

    if len(types) > 1:
        return {'anyOf': [{'type': t} for t in types] + [{'type': 'null'}]}

    value_schema: dict = {'type': types[0], 'nullable': True}

    if data_type == 'multi_label_categorical':
        item_schema: dict = {'type': 'string'}
        if has_resolved_enum:
            item_schema['enum'] = list(field_def['allowed_values'])
        value_schema['items'] = item_schema
    elif has_resolved_enum:
        # Only constrain with a closed enum where the eDAR spec actually defines one
        # (docs/open-decisions.md - most categorical fields don't yet; those stay
        # free-text below rather than a fabricated enum - source instructions §17).
        # No explicit `None` member - `nullable: True` above already covers null.
        value_schema['enum'] = list(field_def['allowed_values'])

    return value_schema


def _field_wrapper_schema(field_def: dict) -> dict:
    """Every extractable field is wrapped the same way: {value, confidence,
    evidence} - uniform regardless of data type, so persistence
    (csc_apps.processing.extraction_service) can treat every field identically."""
    return {
        'type': 'object',
        'properties': {
            'value': _value_schema(field_def),
            'confidence': {'type': 'number', 'nullable': True, 'minimum': 0, 'maximum': 1},
            'evidence': {'type': 'string', 'nullable': True},
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


def build_flat_response_schema(edar_schema: dict) -> dict:
    """Gemini `response_json_schema` for the first of three extraction calls - every
    Module A(minus GPS)/B/C/F/G field as a top-level wrapped property. Split from
    vehicles/casualties into its own call (docs/phase4-gemini-edar-extraction.md
    §Structured-output strategy): the single combined schema this used to be is
    rejected outright by Gemini (HTTP 400) once vehicles and casualties are added
    alongside these 28 fields - verified empirically 2026-09 against the real API,
    independent of any individual field or keyword. Three smaller schemas, one per
    call, each comfortably under Gemini's complexity ceiling."""
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

    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'propertyOrdering': ordering,
        'additionalProperties': False,
    }


def build_vehicles_response_schema(edar_schema: dict) -> dict:
    """The second of three calls - a single `vehicles` key (max 3, Module D)."""
    vehicle_module = next(m for m in edar_schema['modules'] if m['module_id'] == 'D')
    vehicles_schema = {
        'type': 'array',
        'maxItems': vehicle_module['max_repetitions'],
        'items': _repeating_item_schema(vehicle_module),
    }
    return {
        'type': 'object',
        'properties': {'vehicles': vehicles_schema},
        'required': ['vehicles'],
        'propertyOrdering': ['vehicles'],
        'additionalProperties': False,
    }


def build_casualties_response_schema(edar_schema: dict) -> dict:
    """The third of three calls - a single `casualties` key (max MAX_CASUALTIES, an
    engineering safety bound - not a product/eDAR rule - Module E)."""
    casualty_module = next(m for m in edar_schema['modules'] if m['module_id'] == 'E')
    casualties_schema = {
        'type': 'array',
        'maxItems': MAX_CASUALTIES,
        'items': _repeating_item_schema(casualty_module),
    }
    return {
        'type': 'object',
        'properties': {'casualties': casualties_schema},
        'required': ['casualties'],
        'propertyOrdering': ['casualties'],
        'additionalProperties': False,
    }


def _partial_repeating_item_schema(module: dict, base_keys: list[str]) -> dict:
    """Same shape as _repeating_item_schema, restricted to `base_keys` - used by
    build_targeted_response_schema (Phase 10B, docs/phase10b-supplemental-audio.md
    §Targeted extraction) so a supplemental call's vehicles/casualties item schema
    only asks about the specific sub-fields that are actually eligible, not every
    field of that module."""
    fields_by_key = {f['field_key']: f for f in module['fields']}
    properties, required, ordering = {}, [], []
    for key in base_keys:
        properties[key] = _field_wrapper_schema(fields_by_key[key])
        required.append(key)
        ordering.append(key)
    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'propertyOrdering': ordering,
        'additionalProperties': False,
    }


def build_targeted_response_schema(edar_schema: dict, field_keys: list[str]) -> dict:
    """Gemini `response_json_schema` for a Phase 10B supplemental-audio call
    (docs/phase10b-supplemental-audio.md §Targeted extraction) - covers exactly
    `field_keys` (backend-derived, never client-supplied), not the fixed flat/
    vehicles/casualties three-call split the normal extraction uses: a small,
    ad-hoc, per-request schema like this is always far under Gemini's schema-
    complexity ceiling regardless of how many fields it names, so one call is
    enough no matter which fields are eligible.

    `field_keys` may mix flat keys ("weather_at_time") and repeating-entity keys
    ("vehicle.1.registration_number", "casualty.2.injury_severity"). Flat keys
    become top-level properties; repeating keys are grouped into `vehicles`/
    `casualties` arrays capped at the highest eligible index for that entity (never
    the schema's full max_repetitions) - these entities were already identified by
    the original extraction, so this call is only ever asked to fill in missing
    attributes of an *existing* vehicle/casualty slot, never to discover a new one."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    flat_keys = [k for k in field_keys if '.' not in k]

    def _repeating(entity: str) -> tuple[list[int], list[str]]:
        indices, base_keys = set(), set()
        for key in field_keys:
            parts = key.split('.')
            if len(parts) == 3 and parts[0] == entity:
                indices.add(int(parts[1]))
                base_keys.add(parts[2])
        return sorted(indices), sorted(base_keys)

    vehicle_indices, vehicle_base_keys = _repeating('vehicle')
    casualty_indices, casualty_base_keys = _repeating('casualty')

    properties: dict = {}
    required: list[str] = []
    ordering: list[str] = []

    for key in flat_keys:
        for module in edar_schema['modules']:
            if module['repeatable']:
                continue
            field_def = next((f for f in module['fields'] if f['field_key'] == key), None)
            if field_def is not None:
                properties[key] = _field_wrapper_schema(field_def)
                required.append(key)
                ordering.append(key)
                break

    if vehicle_base_keys:
        properties['vehicles'] = {
            'type': 'array',
            'maxItems': max(vehicle_indices),
            'items': _partial_repeating_item_schema(modules_by_id['D'], vehicle_base_keys),
        }
        required.append('vehicles')
        ordering.append('vehicles')

    if casualty_base_keys:
        properties['casualties'] = {
            'type': 'array',
            'maxItems': max(casualty_indices),
            'items': _partial_repeating_item_schema(modules_by_id['E'], casualty_base_keys),
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
