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
