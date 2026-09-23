from unittest.mock import patch

from django.test import TestCase

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar import approval_service
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.schema_validation import EdarValidationError, validate_approved_value
from csc_apps.recordings.models.recording import Recording


def _ai_row(edar_record, field_key, known='KNOWN', value=None, confidence=0.9, evidence='some evidence'):
    return EdarFieldValue.objects.create(
        edar_record=edar_record, field_key=field_key, layer='AI', known=known, value=value,
        confidence=confidence if known == 'KNOWN' else None,
        source_transcript_segment=evidence if known == 'KNOWN' else None,
        extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
    )


class ApprovalServiceBase(TestCase):
    """docs/phase6-officer-review-approval.md §Testing - a hand-built AI candidate
    (not a real extraction run), since approval only cares about EdarFieldValue rows
    already existing with layer=AI, independent of how they got there."""

    def setUp(self):
        self.officer = User.objects.create_user(email='officer6@example.com', password='pw', name='O', role='OFFICER')
        self.other = User.objects.create_user(email='other6@example.com', password='pw', name='X', role='OFFICER')
        self.reviewer = User.objects.create_user(email='rev6@example.com', password='pw', name='R', role='REVIEWER')
        self.recording = Recording.objects.create(officer=self.officer, status='READY_FOR_REVIEW')
        self.edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self.road_name = _ai_row(self.edar_record, 'road_name', value='NH 48', evidence='The crash occurred on NH 48.')
        _ai_row(self.edar_record, 'crash_date', value='2026-03-12')
        _ai_row(self.edar_record, 'hit_and_run_flag', value=False)
        _ai_row(self.edar_record, 'weather_at_time_of_crash', known='UNKNOWN')
        _ai_row(self.edar_record, 'vehicle.1.vehicle_type', value='motorcycle')
        _ai_row(self.edar_record, 'vehicle.1.owner_driver_same_person', known='UNKNOWN')
        _ai_row(self.edar_record, 'casualty.1.person_type', value='rider')
        _ai_row(self.edar_record, 'casualty.1.injury_severity', value='grievous injury')
        self.ai_row_count = EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='AI').count()

    def ai_snapshot(self):
        """Every AI-relevant column, per row, for the immutability comparison."""
        return {
            r.field_value_id: (r.field_key, r.layer, r.known, r.value, r.confidence,
                                r.source_transcript_segment, r.source_start_time, r.source_end_time,
                                r.extraction_version)
            for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='AI')
        }


class MandatoryAcceptanceTests(ApprovalServiceBase):
    """The example acceptance test from the source instructions §30 - mandatory."""

    def test_edited_field_diverges_ai_untouched(self):
        before = self.ai_snapshot()
        result = approval_service.approve_edar(
            edar_record=self.edar_record, officer=self.officer,
            edits={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}},
        )
        self.road_name.refresh_from_db()
        self.assertEqual(self.road_name.value, 'NH 48')
        self.assertEqual(self.road_name.confidence, 0.9)
        self.assertEqual(self.road_name.source_transcript_segment, 'The crash occurred on NH 48.')

        approved = EdarFieldValue.objects.get(edar_record=self.edar_record, field_key='road_name', layer='APPROVED')
        self.assertEqual(approved.value, 'NH 48, Ahmedabad')
        self.assertEqual(approved.known, 'KNOWN')
        self.assertIsNone(approved.confidence)

        self.assertEqual(self.ai_snapshot(), before)

        change = next(c for c in result.changes if c.field_key == 'road_name')
        self.assertEqual(change.ai_value, 'NH 48')
        self.assertEqual(change.approved_value, 'NH 48, Ahmedabad')

        log = ActivityLog.objects.filter(user=self.officer, model='EdarRecord', action='Update').latest('created_on')
        changed = {c['field']: c for c in log.details['changed_fields']}
        self.assertEqual(changed['road_name']['aiValue'], 'NH 48')
        self.assertEqual(changed['road_name']['approvedValue'], 'NH 48, Ahmedabad')
        self.assertEqual(log.user_id, self.officer.user_id)


class NoEditAcceptanceTests(ApprovalServiceBase):
    """§31 - officer submits nothing (or the same values); both layers must exist,
    identical, with no AI mutation."""

    def test_empty_edits_copies_ai_verbatim(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        ai = {r.field_key: (r.known, r.value) for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='AI')}
        approved = {r.field_key: (r.known, r.value) for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED')}
        self.assertEqual(ai, approved)
        self.assertEqual(len(approved), self.ai_row_count)

    def test_resubmitting_identical_value_is_not_a_change(self):
        result = approval_service.approve_edar(
            edar_record=self.edar_record, officer=self.officer,
            edits={'road_name': {'known': 'KNOWN', 'value': 'NH 48'}},
        )
        self.assertEqual(result.changes, [])


class UnknownPreservationTests(ApprovalServiceBase):
    """§32 - leaving an UNKNOWN field UNKNOWN must not become null/empty/false/zero
    in some new, different way; it stays the same UNKNOWN row shape."""

    def test_unknown_field_left_unknown_preserves_known_state_and_null_value(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        approved = EdarFieldValue.objects.get(
            edar_record=self.edar_record, field_key='weather_at_time_of_crash', layer='APPROVED'
        )
        self.assertEqual(approved.known, 'UNKNOWN')
        self.assertIsNone(approved.value)

    def test_officer_can_turn_unknown_into_known(self):
        approval_service.approve_edar(
            edar_record=self.edar_record, officer=self.officer,
            edits={'weather_at_time_of_crash': {'known': 'KNOWN', 'value': 'clear'}},
        )
        approved = EdarFieldValue.objects.get(
            edar_record=self.edar_record, field_key='weather_at_time_of_crash', layer='APPROVED'
        )
        self.assertEqual((approved.known, approved.value), ('KNOWN', 'clear'))

    def test_officer_can_turn_known_into_unknown(self):
        approval_service.approve_edar(
            edar_record=self.edar_record, officer=self.officer,
            edits={'crash_date': {'known': 'UNKNOWN', 'value': None}},
        )
        approved = EdarFieldValue.objects.get(edar_record=self.edar_record, field_key='crash_date', layer='APPROVED')
        self.assertEqual((approved.known, approved.value), ('UNKNOWN', None))


class MultipleFieldEditTests(ApprovalServiceBase):
    """§33 - scalar + boolean + a nested vehicle/casualty field, in one approval."""

    def test_multiple_edits_across_scalar_boolean_and_nested_fields(self):
        before = self.ai_snapshot()
        result = approval_service.approve_edar(
            edar_record=self.edar_record, officer=self.officer,
            edits={
                'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'},
                'hit_and_run_flag': {'known': 'KNOWN', 'value': True},
                'vehicle.1.vehicle_type': {'known': 'KNOWN', 'value': 'car'},
            },
        )
        self.assertEqual(self.ai_snapshot(), before)
        approved = {
            r.field_key: (r.known, r.value)
            for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED')
        }
        self.assertEqual(approved['road_name'], ('KNOWN', 'NH 48, Ahmedabad'))
        self.assertEqual(approved['hit_and_run_flag'], ('KNOWN', True))
        self.assertEqual(approved['vehicle.1.vehicle_type'], ('KNOWN', 'car'))
        # Untouched fields still copied through, full logical dataset preserved.
        self.assertEqual(approved['casualty.1.person_type'], ('KNOWN', 'rider'))
        self.assertEqual(len(result.changes), 3)
        self.assertEqual({c.field_key for c in result.changes}, {'road_name', 'hit_and_run_flag', 'vehicle.1.vehicle_type'})


class InvalidInputTests(ApprovalServiceBase):
    """§37 - invalid officer input must reject the whole approval, atomically."""

    def test_invalid_date_format_rejected_no_partial_write(self):
        with self.assertRaises(EdarValidationError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.officer,
                edits={'crash_date': {'known': 'KNOWN', 'value': 'March 12, 2026'}},
            )
        self.assertFalse(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED').exists())
        self.edar_record.refresh_from_db()
        self.assertEqual(self.edar_record.review_status, 'PENDING_REVIEW')

    def test_invalid_boolean_type_rejected(self):
        with self.assertRaises(EdarValidationError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.officer,
                edits={'hit_and_run_flag': {'known': 'KNOWN', 'value': 'yes'}},
            )

    def test_field_key_not_on_ai_candidate_rejected(self):
        with self.assertRaises(ValueError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.officer,
                edits={'vehicle.2.vehicle_type': {'known': 'KNOWN', 'value': 'truck'}},
            )
        self.assertFalse(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED').exists())

    def test_known_state_requires_non_null_value(self):
        with self.assertRaises(EdarValidationError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.officer,
                edits={'road_name': {'known': 'KNOWN', 'value': None}},
            )

    def test_non_known_state_must_have_null_value(self):
        with self.assertRaises(EdarValidationError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.officer,
                edits={'road_name': {'known': 'UNKNOWN', 'value': 'NH 48'}},
            )


class NoAiCandidateTests(TestCase):
    """§35 - approval on a recording with no AI eDAR candidate at all."""

    def test_no_ai_rows_rejected(self):
        officer = User.objects.create_user(email='o35@example.com', password='pw', name='O', role='OFFICER')
        recording = Recording.objects.create(officer=officer, status='PROCESSING')
        edar_record = EdarRecord.objects.create(recording=recording)
        with self.assertRaises(ValueError):
            approval_service.approve_edar(edar_record=edar_record, officer=officer, edits={})
        self.assertFalse(EdarFieldValue.objects.filter(edar_record=edar_record).exists())


class AlreadyApprovedTests(ApprovalServiceBase):
    """§36 - deterministic rejection, no silent duplicate APPROVED snapshot."""

    def test_second_approval_rejected_no_duplicate_rows(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        first_snapshot = {
            r.field_value_id: r.value for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED')
        }
        with self.assertRaises(ValueError):
            approval_service.approve_edar(
                edar_record=self.edar_record, officer=self.reviewer,
                edits={'road_name': {'known': 'KNOWN', 'value': 'something else'}},
            )
        second_snapshot = {
            r.field_value_id: r.value for r in EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED')
        }
        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED').count(), self.ai_row_count)


class RecordingStateMachineTests(ApprovalServiceBase):
    def test_approval_advances_recording_to_completed(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'COMPLETED')

    def test_legacy_candidate_still_at_processing_advances_through_full_path(self):
        # A candidate extracted before this pipeline wired up the READY_FOR_REVIEW
        # transition (or otherwise never advanced) must still reach COMPLETED, not
        # raise, when approved.
        self.recording.status = 'PROCESSING'
        self.recording.save(update_fields=['status'])
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'COMPLETED')


class AuditTrailTests(ApprovalServiceBase):
    def test_approval_audit_entry_identifies_officer_recording_and_timestamp(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        log = ActivityLog.objects.filter(user=self.officer, model='EdarRecord', action='Update').latest('created_on')
        self.assertEqual(log.details['recording_id'], self.recording.recording_id)
        self.assertEqual(log.details['edar_record_id'], self.edar_record.edar_record_id)
        self.assertIsNotNone(log.created_on)

    def test_reviewed_by_and_reviewed_at_are_server_determined(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.reviewer, edits={})
        self.edar_record.refresh_from_db()
        self.assertEqual(self.edar_record.reviewed_by_id, self.reviewer.user_id)
        self.assertIsNotNone(self.edar_record.reviewed_at)


class TransactionRollbackTests(ApprovalServiceBase):
    """§38 - a failure mid-transaction must leave no partial APPROVED rows and AI
    untouched; review_status/audit must not falsely claim approval."""

    def test_failure_writing_audit_rolls_back_everything(self):
        before = self.ai_snapshot()
        with patch('csc_apps.edar.approval_service.ActivityLog.record', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                approval_service.approve_edar(
                    edar_record=self.edar_record, officer=self.officer,
                    edits={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}},
                )
        self.assertFalse(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED').exists())
        self.edar_record.refresh_from_db()
        self.assertEqual(self.edar_record.review_status, 'PENDING_REVIEW')
        self.assertIsNone(self.edar_record.reviewed_by_id)
        self.recording.refresh_from_db()
        self.assertNotEqual(self.recording.status, 'COMPLETED')
        self.assertEqual(self.ai_snapshot(), before)


class FullLogicalDatasetTests(ApprovalServiceBase):
    """§44 - the approved snapshot must cover the complete logical eDAR dataset the
    AI candidate represents (scalar + vehicle + casualty), without hardcoding an
    assumed row count independent of what the fixture actually has."""

    def test_approved_row_count_matches_ai_row_count_exactly(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})
        ai_keys = set(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='AI').values_list('field_key', flat=True))
        approved_keys = set(EdarFieldValue.objects.filter(edar_record=self.edar_record, layer='APPROVED').values_list('field_key', flat=True))
        self.assertEqual(ai_keys, approved_keys)
        self.assertIn('vehicle.1.vehicle_type', approved_keys)
        self.assertIn('casualty.1.injury_severity', approved_keys)


class ValidateApprovedValueUnitTests(TestCase):
    """Direct unit tests of the reused validation function, mirroring
    csc_apps.edar.test_quality_validation's style for schema_validation coverage."""

    def test_valid_known_value_passes(self):
        validate_approved_value('crash_date', 'KNOWN', '2026-03-12')

    def test_valid_unknown_passes(self):
        validate_approved_value('crash_date', 'UNKNOWN', None)

    def test_known_with_null_value_raises_missing_value(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('crash_date', 'KNOWN', None)
        self.assertEqual(ctx.exception.code, 'missing_value')

    def test_unknown_with_non_null_value_raises_value_not_allowed(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('crash_date', 'UNKNOWN', '2026-03-12')
        self.assertEqual(ctx.exception.code, 'value_not_allowed')

    def test_invalid_known_state_rejected(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('crash_date', 'MAYBE', None)
        self.assertEqual(ctx.exception.code, 'invalid_known_state')

    def test_invalid_field_key_rejected(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('not_a_real_field', 'KNOWN', 'x')
        self.assertEqual(ctx.exception.code, 'invalid_field_key')

    def test_invalid_type_rejected(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('number_of_vehicles_involved', 'KNOWN', 'two')
        self.assertEqual(ctx.exception.code, 'invalid_type')

    def test_resolved_enum_enforced(self):
        with self.assertRaises(EdarValidationError) as ctx:
            validate_approved_value('casualty.1.injury_severity', 'KNOWN', 'catastrophic')
        self.assertEqual(ctx.exception.code, 'invalid_enum')
        validate_approved_value('casualty.1.injury_severity', 'KNOWN', 'fatal')
