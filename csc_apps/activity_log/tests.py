from django.test import TestCase

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User


class ActivityLogRecordTests(TestCase):
    """ActivityLog.record() must persist a queryable audit row
    (docs/observability.md §1)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='admin@example.com', password='pw', name='Admin One', role='ADMIN'
        )

    def test_record_persists_action_and_details(self):
        ActivityLog.record(
            user=self.user, action='Update', model='EdarFieldValue', details={'field_key': 'crash_date'}
        )
        log = ActivityLog.objects.get()
        self.assertEqual(log.user_id, self.user.user_id)
        self.assertEqual(log.action, 'Update')
        self.assertEqual(log.details, {'field_key': 'crash_date'})

    def test_read_action_is_a_valid_choice(self):
        # Phase 9 (docs/phase9-security-audit-observability.md) - login and export
        # auditing both use this fourth, generic CRUD-style verb rather than a
        # one-off action name per event type.
        log = ActivityLog.record(user=self.user, action='Read', model='User', details={'event': 'login'})
        self.assertIn('Read', dict(ActivityLog.ACTION_CHOICES))
        self.assertEqual(log.action, 'Read')
