from django.db import models

from csc_apps.authentication.models import User
from csc_apps.recordings.models.recording import Recording


class EdarRecord(models.Model):
    """Header row for one Recording's eDAR data (docs/domain-model.md §3). All 42
    field values live on EdarFieldValue (ADR-011), not here."""

    REVIEW_STATUS_CHOICES = [
        ('PENDING_REVIEW', 'Pending Review'),
        ('IN_REVIEW', 'In Review'),
        ('APPROVED', 'Approved'),
    ]

    edar_record_id = models.AutoField(primary_key=True)
    recording = models.OneToOneField(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='edar_record'
    )
    review_status = models.CharField(
        verbose_name='Review Status', choices=REVIEW_STATUS_CHOICES, max_length=20, default='PENDING_REVIEW'
    )
    reviewed_by = models.ForeignKey(
        verbose_name='Reviewed By', to=User, on_delete=models.PROTECT, null=True, blank=True
    )
    reviewed_at = models.DateTimeField(verbose_name='Reviewed At', null=True, blank=True)

    class Meta:
        db_table = 'edar_records'

    def __str__(self) -> str:
        return f'eDAR Record for Recording #{self.recording_id} ({self.review_status})'
