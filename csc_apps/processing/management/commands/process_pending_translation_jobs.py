"""Runs every runnable TRANSLATION ProcessingJob against Sarvam.

Kept as a separate command from process_pending_stt_jobs (docs/phase3-sarvam-
translation.md §Task execution) rather than folded into one combined command: STT and
translation are independently retryable stages (docs/phase3-sarvam-translation.md
§Translation job lifecycle), and a translation-only backlog (e.g. Sarvam's
translation endpoint degraded while STT is healthy) can be worked through - or an
operator can pause it - without touching the STT cron entry at all.
"""

from django.core.management.base import BaseCommand

from csc_apps.processing.models import ProcessingJob
from csc_apps.processing.translation_service import run_translation_job


class Command(BaseCommand):
    help = 'Runs every PENDING/RETRYING TRANSLATION ProcessingJob against Sarvam (docs/phase3-sarvam-translation.md).'

    def handle(self, *args, **options):
        job_ids = list(
            ProcessingJob.objects.filter(job_type='TRANSLATION', status__in=['PENDING', 'RETRYING'])
            .order_by('created_at')
            .values_list('job_id', flat=True)
        )
        self.stdout.write(f'Found {len(job_ids)} runnable TRANSLATION job(s)')

        for job_id in job_ids:
            try:
                job = run_translation_job(job_id)
            except Exception as e:  # noqa: BLE001 - one job's unexpected failure must not stop the batch
                self.stderr.write(f'job_id={job_id} raised unexpectedly: {e}')
                continue
            self.stdout.write(f'job_id={job_id} -> {job.status}')
