from django.db import models

from csc_apps.authentication.models import User
from csc_apps.edar.models.edar_record import EdarRecord


class EdarFieldValue(models.Model):
    """One value for one eDAR field, one layer (ADR-008, ADR-011). `field_key` is a
    base field ("crash_date") or a repeating-group entry ("vehicle.1.vehicle_type",
    "casualty.2.injury_severity") validated against schemas/edar-schema.json by
    csc_apps.edar.schema_validation before a row is written.
    """

    LAYER_CHOICES = [
        ('AI', 'AI Extracted'),
        ('APPROVED', 'Officer Approved'),
    ]

    KNOWN_CHOICES = [
        ('KNOWN', 'Known'),
        ('UNKNOWN', 'Unknown'),
        ('NOT_APPLICABLE', 'Not Applicable'),
        ('UNCERTAIN', 'Uncertain'),
    ]

    field_value_id = models.AutoField(primary_key=True)
    edar_record = models.ForeignKey(
        verbose_name='eDAR Record', to=EdarRecord, on_delete=models.PROTECT, related_name='field_values'
    )
    field_key = models.CharField(verbose_name='Field Key', max_length=100)
    layer = models.CharField(verbose_name='Layer', choices=LAYER_CHOICES, max_length=10)
    known = models.CharField(verbose_name='Known', choices=KNOWN_CHOICES, max_length=20, default='KNOWN')
    value = models.JSONField(verbose_name='Value', null=True, blank=True)

    # AI layer only (ADR-010, docs/data-provenance.md §2).
    confidence = models.FloatField(verbose_name='Confidence', null=True, blank=True)
    source_transcript_segment = models.TextField(verbose_name='Source Transcript Segment', null=True, blank=True)
    source_start_time = models.FloatField(verbose_name='Source Start Time', null=True, blank=True)
    source_end_time = models.FloatField(verbose_name='Source End Time', null=True, blank=True)
    extraction_version = models.CharField(verbose_name='Extraction Version', max_length=100, null=True, blank=True)

    # APPROVED layer only.
    updated_by = models.ForeignKey(
        verbose_name='Updated By', to=User, on_delete=models.PROTECT, null=True, blank=True
    )
    updated_at = models.DateTimeField(verbose_name='Updated At', auto_now=True)

    class Meta:
        db_table = 'edar_field_values'
        unique_together = [('edar_record', 'field_key', 'layer')]

    def __str__(self) -> str:
        return f'{self.field_key} ({self.layer}) - Record #{self.edar_record_id}'
