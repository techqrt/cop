from django.db import models

from csc_apps.authentication.models import User


class Recording(models.Model):
    """One crash-report capture session (docs/domain-model.md §3). Owns the lifecycle
    state (docs/recording-state-machine.md) and the device-captured GPS (ADR-012).
    """

    STATUS_CHOICES = [
        ('CREATED', 'Created'),
        ('RECORDING', 'Recording'),
        ('UPLOADED', 'Uploaded'),
        ('PROCESSING', 'Processing'),
        ('READY_FOR_REVIEW', 'Ready For Review'),
        ('IN_REVIEW', 'In Review'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('RETRY', 'Retry'),
    ]

    recording_id = models.AutoField(primary_key=True)
    officer = models.ForeignKey(
        verbose_name='Officer', to=User, on_delete=models.PROTECT, related_name='recordings'
    )
    status = models.CharField(verbose_name='Status', choices=STATUS_CHOICES, max_length=20, default='CREATED')

    # Device-captured, never written by the AI pipeline (ADR-012).
    gps_latitude = models.FloatField(verbose_name='GPS Latitude', null=True, blank=True)
    gps_longitude = models.FloatField(verbose_name='GPS Longitude', null=True, blank=True)
    gps_captured_at = models.DateTimeField(verbose_name='GPS Captured At', null=True, blank=True)

    # Module A record-identity fields (docs/domain-model.md §3 note) - officer-confirmed,
    # not primarily an extraction target.
    road_name = models.CharField(verbose_name='Road Name', max_length=255, null=True, blank=True)
    police_station_jurisdiction = models.CharField(
        verbose_name='Police Station Jurisdiction', max_length=255, null=True, blank=True
    )
    case_fir_number = models.CharField(verbose_name='Case / FIR Number', max_length=100, null=True, blank=True)

    created_at = models.DateTimeField(verbose_name='Created At', auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name='Updated At', auto_now=True)

    class Meta:
        db_table = 'recordings'

    def __str__(self) -> str:
        return f'Recording #{self.recording_id} ({self.status})'
