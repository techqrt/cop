from django.test import SimpleTestCase
from jsonschema import Draft202012Validator

from csc_apps.edar.schema_loader import load_schema
from csc_apps.edar.schema_validation import resolve_field_key, validate_extraction_entry
from csc_apps.processing.providers.gemini.schema_adapter import (
    GPS_FIELD_KEY,
    MAX_CASUALTIES,
    build_response_schema,
    flat_field_keys,
    repeating_field_keys,
)

# Structural meta-schema for schemas/edar-schema.json itself (not for validating
# extraction results - see schema_validation.py for that). Confirms every module
# entry has the shape the rest of the codebase assumes.
_EDAR_SCHEMA_META_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object',
    'required': ['field_count', 'max_vehicles', 'modules'],
    'properties': {
        'field_count': {'const': 42},
        'max_vehicles': {'const': 3},
        'modules': {
            'type': 'array',
            'items': {
                'type': 'object',
                'required': ['module_id', 'name', 'repeatable', 'field_count', 'fields'],
                'properties': {
                    'module_id': {'enum': ['A', 'B', 'C', 'D', 'E', 'F', 'G']},
                    'fields': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'required': ['field_key', 'name', 'data_type'],
                        },
                    },
                },
            },
        },
    },
}


class EdarSchemaFileTests(SimpleTestCase):
    """docs/ai-extraction-contract.md, ADR-006 - the schema file is the single source
    of truth: exactly 42 fields across exactly 7 modules (A-G), max 3 vehicles."""

    def test_schema_file_matches_its_own_meta_schema(self):
        schema = load_schema()
        Draft202012Validator(_EDAR_SCHEMA_META_SCHEMA).validate(schema)

    def test_field_count_is_42_across_seven_modules(self):
        schema = load_schema()
        self.assertEqual(len(schema['modules']), 7)
        total_fields = sum(module['field_count'] for module in schema['modules'])
        self.assertEqual(total_fields, 42)
        for module in schema['modules']:
            self.assertEqual(len(module['fields']), module['field_count'])

    def test_vehicle_module_caps_at_three(self):
        schema = load_schema()
        vehicle_module = next(m for m in schema['modules'] if m.get('repeat_entity') == 'vehicle')
        self.assertEqual(vehicle_module['max_repetitions'], 3)

    def test_casualty_module_is_unbounded(self):
        schema = load_schema()
        casualty_module = next(m for m in schema['modules'] if m.get('repeat_entity') == 'casualty')
        self.assertIsNone(casualty_module['max_repetitions'])


class ResolveFieldKeyTests(SimpleTestCase):
    def test_resolves_module_a_field(self):
        field = resolve_field_key('crash_date')
        self.assertEqual(field['name'], 'Crash date')

    def test_resolves_vehicle_field_within_bounds(self):
        field = resolve_field_key('vehicle.2.vehicle_type')
        self.assertEqual(field['field_key'], 'vehicle_type')

    def test_rejects_vehicle_index_beyond_max_repetitions(self):
        with self.assertRaises(ValueError):
            resolve_field_key('vehicle.4.vehicle_type')

    def test_rejects_unknown_field(self):
        with self.assertRaises(ValueError):
            resolve_field_key('not_a_real_field')

    def test_casualty_field_has_no_upper_index_bound(self):
        field = resolve_field_key('casualty.11.injury_severity')
        self.assertEqual(field['field_key'], 'injury_severity')


class ValidateExtractionEntryTests(SimpleTestCase):
    """docs/ai-extraction-contract.md §4 - Schema Validation stage."""

    def _entry(self, **overrides):
        base = {
            'field': 'primary_causation_factor',
            'value': 'overspeeding',
            'confidence': 0.91,
            'source': {
                'transcript_segment': 'the truck was going way too fast',
                'start_time': 182.4,
                'end_time': 187.8,
            },
        }
        base.update(overrides)
        return base

    def test_valid_entry_passes(self):
        validate_extraction_entry(self._entry())

    def test_entry_for_unknown_field_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry(self._entry(field='not_a_real_field'))

    def test_boolean_field_rejects_non_boolean_value(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry(self._entry(field='hit_and_run_flag', value='yes'))

    def test_confidence_without_source_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry(self._entry(source=None))

    def test_confidence_out_of_range_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry(self._entry(confidence=1.5))

    def test_source_start_after_end_fails(self):
        entry = self._entry()
        entry['source']['start_time'] = 200.0
        with self.assertRaises(ValueError):
            validate_extraction_entry(entry)

    def test_no_evidence_omits_confidence_and_source_and_passes(self):
        validate_extraction_entry({'field': 'crash_date', 'value': None, 'confidence': None, 'source': None})

    def test_missing_start_end_time_is_accepted(self):
        # Phase 4's Gemini-based extraction has no audio timestamps - only a text
        # excerpt (docs/phase4-gemini-edar-extraction.md §Provenance).
        validate_extraction_entry({
            'field': 'road_name', 'value': 'NH 48', 'confidence': 0.9,
            'source': {'transcript_segment': 'on NH 48 near the toll', 'start_time': None, 'end_time': None},
        })

    def test_valid_date_passes(self):
        validate_extraction_entry({
            'field': 'crash_date', 'value': '2026-05-14', 'confidence': 0.95,
            'source': {'transcript_segment': 'today, 14 May', 'start_time': None, 'end_time': None},
        })

    def test_invalid_date_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'crash_date', 'value': 'not-a-date', 'confidence': 0.9,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })

    def test_valid_time_passes(self):
        validate_extraction_entry({
            'field': 'crash_time', 'value': '08:30', 'confidence': 0.9,
            'source': {'transcript_segment': 'saade aath baje', 'start_time': None, 'end_time': None},
        })

    def test_invalid_time_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'crash_time', 'value': '25:99', 'confidence': 0.9,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })

    def test_integer_field_rejects_string(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'speed_limit_on_road', 'value': '60', 'confidence': 0.8,
                'source': {'transcript_segment': '60 km/h zone', 'start_time': None, 'end_time': None},
            })

    def test_integer_field_rejects_bool(self):
        # bool is a subclass of int in Python - must not slip through an isinstance(value, int) check.
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'speed_limit_on_road', 'value': True, 'confidence': 0.8,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })

    def test_multi_label_field_rejects_non_list(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'road_features_present', 'value': 'divider present', 'confidence': 0.7,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })

    def test_multi_label_field_accepts_list(self):
        validate_extraction_entry({
            'field': 'road_features_present', 'value': ['divider present'], 'confidence': 0.7,
            'source': {'transcript_segment': 'there was a divider', 'start_time': None, 'end_time': None},
        })

    def test_resolved_enum_field_accepts_allowed_value(self):
        validate_extraction_entry({
            'field': 'casualty.1.injury_severity', 'value': 'minor injury', 'confidence': 0.85,
            'source': {'transcript_segment': 'minor injuries', 'start_time': None, 'end_time': None},
        })

    def test_resolved_enum_field_rejects_invented_value(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'casualty.1.injury_severity', 'value': 'probably fine', 'confidence': 0.5,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })

    def test_unresolved_categorical_field_accepts_free_text(self):
        # road_type has no defined enum yet (docs/open-decisions.md) - any descriptive
        # string is accepted, not rejected against a nonexistent closed list.
        validate_extraction_entry({
            'field': 'road_type', 'value': 'National Highway', 'confidence': 0.8,
            'source': {'transcript_segment': 'on the national highway', 'start_time': None, 'end_time': None},
        })

    def test_vehicle_field_within_bounds_validates(self):
        validate_extraction_entry({
            'field': 'vehicle.2.vehicle_type', 'value': 'truck', 'confidence': 0.8,
            'source': {'transcript_segment': 'the truck', 'start_time': None, 'end_time': None},
        })

    def test_vehicle_field_beyond_max_repetitions_fails(self):
        with self.assertRaises(ValueError):
            validate_extraction_entry({
                'field': 'vehicle.4.vehicle_type', 'value': 'truck', 'confidence': 0.8,
                'source': {'transcript_segment': 'x', 'start_time': None, 'end_time': None},
            })


class GeminiSchemaAdapterTests(SimpleTestCase):
    """docs/phase4-gemini-edar-extraction.md §eDAR schema integration - the Gemini
    response schema is derived from schemas/edar-schema.json, not a second,
    independently-maintained schema (source instructions §77). Every field key this
    adapter emits must still resolve via csc_apps.edar.schema_validation.
    resolve_field_key - the alignment guarantee."""

    def test_flat_field_keys_excludes_gps_and_matches_expected_count(self):
        schema = load_schema()
        keys = flat_field_keys(schema)
        self.assertNotIn(GPS_FIELD_KEY, keys)
        # Module A (6, minus GPS) + B(8) + C(7) + F(5) + G(3) = 28.
        self.assertEqual(len(keys), 28)

    def test_flat_field_keys_all_resolve_via_schema_validation(self):
        schema = load_schema()
        for key in flat_field_keys(schema):
            resolve_field_key(key, schema=schema)  # must not raise

    def test_repeating_field_keys_match_module_field_counts(self):
        schema = load_schema()
        self.assertEqual(len(repeating_field_keys(schema, 'vehicle')), 7)
        self.assertEqual(len(repeating_field_keys(schema, 'casualty')), 6)

    def test_response_schema_top_level_shape(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        self.assertEqual(response_schema['type'], 'object')
        for key in flat_field_keys(schema):
            self.assertIn(key, response_schema['properties'])
            self.assertIn(key, response_schema['required'])
        self.assertIn('vehicles', response_schema['properties'])
        self.assertIn('casualties', response_schema['properties'])

    def test_vehicles_array_capped_at_three(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        self.assertEqual(response_schema['properties']['vehicles']['maxItems'], 3)

    def test_casualties_array_capped_at_safety_bound(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        self.assertEqual(response_schema['properties']['casualties']['maxItems'], MAX_CASUALTIES)

    def test_every_field_wrapper_has_value_confidence_evidence(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        for key in flat_field_keys(schema):
            wrapper = response_schema['properties'][key]
            self.assertEqual(set(wrapper['properties']), {'value', 'confidence', 'evidence'})

    def test_resolved_enum_field_gets_enum_constraint(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        casualty_item = response_schema['properties']['casualties']['items']
        injury_value_schema = casualty_item['properties']['injury_severity']['properties']['value']
        self.assertIn('enum', injury_value_schema)
        self.assertIn('fatal', injury_value_schema['enum'])

    def test_unresolved_categorical_field_has_no_enum_constraint(self):
        schema = load_schema()
        response_schema = build_response_schema(schema)
        road_type_value_schema = response_schema['properties']['road_type']['properties']['value']
        self.assertNotIn('enum', road_type_value_schema)

    def test_schema_is_json_serializable(self):
        import json

        schema = load_schema()
        json.dumps(build_response_schema(schema))  # must not raise
