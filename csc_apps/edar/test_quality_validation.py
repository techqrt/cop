from django.test import SimpleTestCase

from csc_apps.edar.quality_validation import (
    EVIDENCE_MATCH_RULE, LOW_CONFIDENCE_THRESHOLD, STATUS_INVALID, STATUS_VALIDATED, STATUS_VALIDATION_WARNING,
    assess_candidate, is_evidence_traceable, normalize_for_match,
)
from csc_apps.edar.schema_loader import load_schema
from csc_apps.edar.schema_validation import EdarValidationError, validate_extraction_entry

TRANSCRIPT = 'The motorcycle hit the rear of the car at the intersection.  It was raining heavily.'


def entry(field, value, confidence=0.9, evidence='hit the rear of the car'):
    source = {'transcript_segment': evidence} if evidence is not None else None
    return {'field': field, 'value': value, 'confidence': confidence, 'source': source}


def assess(entries, expected=None, vehicles=0, casualties=0, transcript=TRANSCRIPT):
    expected = expected or [e['field'] for e in entries] + ['road_name']
    return assess_candidate(entries, expected, transcript, load_schema(), vehicles, casualties)


class EvidenceMatchingTests(SimpleTestCase):
    def test_exact_evidence_is_traceable(self):
        self.assertTrue(is_evidence_traceable('hit the rear of the car', TRANSCRIPT))

    def test_case_whitespace_and_edge_punctuation_are_normalized(self):
        self.assertTrue(is_evidence_traceable('  "HIT   the rear of the CAR." ', TRANSCRIPT))

    def test_evidence_spanning_double_space_matches(self):
        self.assertTrue(is_evidence_traceable('intersection. It was raining', TRANSCRIPT))

    def test_unicode_normalization_applies(self):
        self.assertEqual(normalize_for_match('ＡＢＣ'), 'abc')

    def test_missing_evidence_text_is_not_traceable(self):
        self.assertFalse(is_evidence_traceable('the truck ran a red light', TRANSCRIPT))
        self.assertFalse(is_evidence_traceable('   ', TRANSCRIPT))

    def test_evidence_from_a_different_transcript_is_not_traceable(self):
        self.assertFalse(is_evidence_traceable('hit the rear of the car', 'A pedestrian was crossing the road.'))

    def test_ellipsis_fragments_must_each_match(self):
        self.assertTrue(is_evidence_traceable('motorcycle hit ... at the intersection', TRANSCRIPT))
        self.assertTrue(is_evidence_traceable('motorcycle hit … intersection', TRANSCRIPT))
        self.assertFalse(is_evidence_traceable('motorcycle hit ... a bus', TRANSCRIPT))

    def test_transcript_argument_is_not_mutated(self):
        original = TRANSCRIPT
        is_evidence_traceable('hit the rear', original)
        self.assertEqual(original, TRANSCRIPT)


class AssessCandidateTests(SimpleTestCase):
    def test_fully_valid_candidate_is_validated(self):
        result = assess([entry('crash_type', 'rear-end collision')])
        self.assertEqual(result.report['status'], STATUS_VALIDATED)
        self.assertEqual(result.report['warnings'], [])
        self.assertEqual({r['field_key']: r['known'] for r in result.rows}, {'crash_type': 'KNOWN', 'road_name': 'UNKNOWN'})

    def test_unknown_field_has_no_value_confidence_or_evidence(self):
        row = next(r for r in assess([entry('crash_type', 'x')]).rows if r['field_key'] == 'road_name')
        self.assertIsNone(row['value'])
        self.assertIsNone(row['confidence'])
        self.assertIsNone(row['source_transcript_segment'])

    def test_untraceable_evidence_is_warning_not_error(self):
        result = assess([entry('crash_type', 'x', evidence='a completely different sentence')])
        self.assertEqual(result.report['status'], STATUS_VALIDATION_WARNING)
        self.assertEqual([w['code'] for w in result.report['warnings']], ['evidence_not_traceable'])
        self.assertEqual(result.report['metrics']['knownFieldsWithVerifiedEvidence'], 0)
        self.assertEqual(result.report['metrics']['knownFieldsWithEvidence'], 1)

    def test_missing_evidence_and_confidence_warn_together(self):
        result = assess([entry('crash_type', 'x', confidence=None, evidence=None)])
        self.assertEqual(result.report['status'], STATUS_VALIDATION_WARNING)
        codes = {w['code'] for w in result.report['warnings']}
        self.assertEqual(codes, {'missing_evidence', 'missing_confidence'})

    def test_low_confidence_is_warning_and_value_is_kept(self):
        result = assess([entry('crash_type', 'x', confidence=LOW_CONFIDENCE_THRESHOLD - 0.01)])
        self.assertEqual(result.report['status'], STATUS_VALIDATION_WARNING)
        self.assertEqual(result.report['metrics']['lowConfidenceFields'], 1)
        self.assertEqual(result.rows[0]['known'], 'KNOWN')

    def test_confidence_boundaries_zero_and_one_are_valid(self):
        for c in (0, 0.0, 1, 1.0):
            self.assertNotEqual(assess([entry('crash_type', 'x', confidence=c)]).report['status'], STATUS_INVALID, c)

    def test_invalid_confidence_values_are_errors_not_clamped(self):
        for c in (-0.1, 1.01, 5, True, 'high', float('nan')):
            result = assess([entry('crash_type', 'x', confidence=c)])
            self.assertEqual(result.report['status'], STATUS_INVALID, c)
            self.assertEqual(result.report['errors'][0]['code'], 'invalid_confidence', c)
            self.assertEqual(result.rows, [])

    def test_confidence_without_evidence_is_error(self):
        result = assess([entry('crash_type', 'x', confidence=0.9, evidence=None)])
        self.assertEqual(result.report['errors'][0]['code'], 'provenance_incomplete')

    def test_unknown_field_key_is_error(self):
        result = assess([entry('not_a_field', 'x')], expected=['road_name'])
        self.assertEqual(result.report['errors'][0]['code'], 'invalid_field_key')

    def test_type_errors(self):
        cases = [
            ('number_of_vehicles_involved', 'two'), ('number_of_vehicles_involved', True),
            ('hit_and_run_flag', 'yes'), ('crash_date', '20/01/2026'), ('crash_time', '25:99'),
            ('traffic_control_device', 'signal'),
        ]
        for field, value in cases:
            result = assess([entry(field, value)])
            self.assertEqual(result.report['status'], STATUS_INVALID, (field, value))
            self.assertEqual(result.report['errors'][0]['code'], 'invalid_type', (field, value))

    def test_valid_typed_values(self):
        for field, value in [
            ('number_of_vehicles_involved', 2), ('hit_and_run_flag', True), ('crash_date', '2026-01-20'),
            ('crash_time', '14:30'), ('traffic_control_device', ['signal', 'sign']),
        ]:
            self.assertNotEqual(assess([entry(field, value)]).report['status'], STATUS_INVALID, field)

    def test_resolved_enum_is_enforced_and_unresolved_categorical_is_free_text(self):
        bad = assess([entry('casualty.1.injury_severity', 'critical')], casualties=1)
        self.assertEqual(bad.report['errors'][0]['code'], 'invalid_enum')
        good = assess([entry('casualty.1.injury_severity', 'fatal')], casualties=1)
        self.assertNotEqual(good.report['status'], STATUS_INVALID)
        free = assess([entry('crash_type', 'anything the model said')])
        self.assertNotEqual(free.report['status'], STATUS_INVALID)
        self.assertEqual(free.report['metrics']['freeTextCategoricalFields'], 1)

    def test_vehicle_limit_exceeded_is_error(self):
        result = assess([], expected=['road_name'], vehicles=4)
        self.assertEqual(result.report['status'], STATUS_INVALID)
        self.assertEqual(result.report['errors'][0]['code'], 'entity_limit_exceeded')

    def test_duplicate_field_is_error(self):
        result = assess([entry('crash_type', 'a'), entry('crash_type', 'b')])
        self.assertEqual(result.report['errors'][0]['code'], 'duplicate_field')

    def test_duplicate_labels_warn(self):
        result = assess([entry('traffic_control_device', ['signal', 'signal'])])
        self.assertIn('duplicate_label', [w['code'] for w in result.report['warnings']])

    def test_vehicle_count_cross_checks_are_warnings(self):
        few = assess([entry('number_of_vehicles_involved', 1), entry('vehicle.1.vehicle_type', 'car'),
                      entry('vehicle.2.vehicle_type', 'bike')], vehicles=2)
        self.assertIn('vehicle_count_mismatch', [w['code'] for w in few.report['warnings']])
        many = assess([entry('number_of_vehicles_involved', 3), entry('vehicle.1.vehicle_type', 'car')], vehicles=1)
        self.assertIn('vehicle_records_incomplete', [w['code'] for w in many.report['warnings']])
        self.assertNotEqual(many.report['status'], STATUS_INVALID)

    def test_person_count_mismatch_warns(self):
        result = assess([entry('number_of_persons_involved', 1), entry('casualty.1.gender', 'm'),
                         entry('casualty.2.gender', 'f')], casualties=2)
        self.assertIn('person_count_mismatch', [w['code'] for w in result.report['warnings']])

    def test_metrics_are_counts_and_coverage_only(self):
        result = assess([entry('crash_type', 'x')], expected=['crash_type', 'road_name', 'crash_date', 'crash_time'])
        m = result.report['metrics']
        self.assertEqual((m['totalFields'], m['knownFields'], m['unknownFields']), (4, 1, 3))
        self.assertEqual(m['fieldCoverageRatio'], 0.25)
        for banned in ('accuracy', 'score', 'overallConfidence', 'averageConfidence'):
            self.assertFalse([k for k in m if banned.lower() in k.lower()])

    def test_report_messages_never_contain_values_or_transcript_text(self):
        result = assess([entry('crash_type', 'SECRET-VALUE', confidence=9, evidence='SECRET-EVIDENCE')])
        self.assertNotIn('SECRET', str(result.report))

    def test_report_states_evidence_rule(self):
        self.assertEqual(assess([]).report['evidenceMatchRule'], EVIDENCE_MATCH_RULE)


class EdarValidationErrorTests(SimpleTestCase):
    def test_error_has_code_and_is_value_error(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_extraction_entry(entry('crash_type', 'x', confidence=2), schema=load_schema())
        self.assertEqual(ctx.exception.code, 'invalid_confidence')
        self.assertIsInstance(ctx.exception, ValueError)
