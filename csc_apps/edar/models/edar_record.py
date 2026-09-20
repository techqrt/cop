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

    # Phase 5 (docs/phase5-validation-provenance.md §Persistence) - AI-candidate
    # provenance and quality, written in the same atomic transaction as the AI
    # EdarFieldValue rows so status and fields can never disagree. None of this means
    # "approved": quality_status VALIDATED/VALIDATION_WARNING describes deterministic
    # validation only; review_status above is untouched by extraction.
    QUALITY_STATUS_CHOICES = [
        ('VALIDATED', 'Validated'),
        ('VALIDATION_WARNING', 'Validation Warning'),
    ]
    quality_status = models.CharField(
        verbose_name='Quality Status', choices=QUALITY_STATUS_CHOICES, max_length=20, null=True, blank=True
    )
    quality_report = models.JSONField(verbose_name='Quality Report', default=dict, blank=True)
    # The exact English Transcript row this AI candidate was extracted from, and the
    # extraction ProcessingJob execution that produced it - existing identities, no new
    # version system (source instructions §27-28).
    source_transcript = models.ForeignKey(
        verbose_name='Source Transcript', to='recordings.Transcript', on_delete=models.PROTECT,
        null=True, blank=True, related_name='+',
    )
    extraction_job = models.ForeignKey(
        verbose_name='Extraction Job', to='processing.ProcessingJob', on_delete=models.PROTECT,
        null=True, blank=True, related_name='+',
    )
    extracted_at = models.DateTimeField(verbose_name='Extracted At', null=True, blank=True)

    class Meta:
        db_table = 'edar_records'

    def __str__(self) -> str:
        return f'eDAR Record for Recording #{self.recording_id} ({self.review_status})'
