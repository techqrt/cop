from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from csc_apps.activity_log.middlewares.log_middleware import local
from csc_apps.authentication.models import User


class ActivityLog(models.Model):
    """CRUD audit trail, ported from pms_apps/activity_log/models/activity_log.py
    (docs/pms-reference-analysis.md §9, docs/observability.md §1). Distinct from
    ProcessingEvent (csc_apps.processing), which tracks pipeline milestones rather
    than who-changed-what.
    """

    ACTION_CHOICES = (
        ('Create', 'Create'),
        ('Update', 'Update'),
        ('Delete', 'Delete'),
    )

    log_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, verbose_name='User', on_delete=models.SET_NULL, null=True)
    ip_address = models.CharField(max_length=45)
    user_agent = models.TextField()
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    model = models.CharField(max_length=50)
    method = models.CharField(max_length=10)
    end_point = models.CharField(max_length=125)
    details = models.JSONField(encoder=DjangoJSONEncoder)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_on']
        db_table = 'activity_log'

    def __str__(self) -> str:
        return f'{self.user.email if self.user else "NA"} - {self.action} {self.model}'

    @staticmethod
    def record(user, action: str, model: str, details: dict) -> 'ActivityLog':
        """Explicit call from a view method at the point of a tracked write - e.g.
        `ActivityLog.record(user=officer, action='Update', model='EdarFieldValue',
        details={...})` when an officer approves a field. PMS drives this off a Django
        signal instead; CSC keeps it explicit for Phase 0 since no domain CRUD
        endpoint exists yet to attach a signal to (docs/pms-reference-analysis.md §9) -
        revisit once Phase 1 adds the endpoints this would actually fire for.
        """
        return ActivityLog.objects.create(
            user=user,
            ip_address=getattr(local, 'ip_address', '') or '',
            user_agent=getattr(local, 'user_agent', '') or '',
            action=action,
            model=model,
            method=getattr(local, 'method', '') or '',
            end_point=getattr(local, 'end_point', '') or '',
            details=details,
        )
