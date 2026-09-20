from django.db import models

from csc_apps.recordings.models.recording import Recording


class ProcessingJob(models.Model):
    """One asynchronous pipeline unit of work (docs/domain-model.md §3,
    docs/error-retry-strategy.md §2)."""

    JOB_TYPE_CHOICES = [
        ('STT', 'Speech To Text'),
        ('TRANSLATION', 'Translation'),
        ('EXTRACTION', 'eDAR Extraction'),
        ('EXPORT', 'Export'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('RUNNING', 'Running'),
        ('SUCCEEDED', 'Succeeded'),
        ('FAILED', 'Failed'),
        ('RETRYING', 'Retrying'),
    ]

    job_id = models.AutoField(primary_key=True)
    recording = models.ForeignKey(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='processing_jobs'
    )
    job_type = models.CharField(verbose_name='Job Type', choices=JOB_TYPE_CHOICES, max_length=20)
    status = models.CharField(verbose_name='Status', choices=STATUS_CHOICES, max_length=10, default='PENDING')

    attempt_count = models.PositiveIntegerField(verbose_name='Attempt Count', default=0)
    max_attempts = models.PositiveIntegerField(verbose_name='Max Attempts', default=3)
    is_retryable = models.BooleanField(verbose_name='Is Retryable', null=True, blank=True)
    error_code = models.CharField(verbose_name='Error Code', max_length=100, null=True, blank=True)
    error_message = models.TextField(verbose_name='Error Message', null=True, blank=True)

    provider_name = models.CharField(verbose_name='Provider Name', max_length=100, null=True, blank=True)
    provider_metadata = models.JSONField(verbose_name='Provider Metadata', default=dict, blank=True)

    started_at = models.DateTimeField(verbose_name='Started At', null=True, blank=True)
    completed_at = models.DateTimeField(verbose_name='Completed At', null=True, blank=True)
    created_at = models.DateTimeField(verbose_name='Created At', auto_now_add=True)

    class Meta:
        db_table = 'processing_jobs'

    def __str__(self) -> str:
        return f'{self.job_type} job #{self.job_id} for Recording #{self.recording_id} ({self.status})'


class ProcessingEvent(models.Model):
    """Immutable pipeline timeline entry (docs/observability.md §1/§2) - distinct from
    csc_apps.activity_log.ActivityLog, which is a CRUD audit trail, not a pipeline
    timeline."""

    event_id = models.AutoField(primary_key=True)
    recording = models.ForeignKey(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='processing_events'
    )
    job = models.ForeignKey(
        verbose_name='Processing Job',
        to=ProcessingJob,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='events',
    )
    event_type = models.CharField(verbose_name='Event Type', max_length=100)
    metadata = models.JSONField(verbose_name='Metadata', default=dict, blank=True)
    occurred_at = models.DateTimeField(verbose_name='Occurred At', auto_now_add=True)

    class Meta:
        db_table = 'processing_events'
        ordering = ['occurred_at']

    def __str__(self) -> str:
        return f'{self.event_type} @ Recording #{self.recording_id}'
