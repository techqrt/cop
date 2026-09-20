"""Runs every runnable EXTRACTION ProcessingJob against Gemini.

Kept as a separate command from process_pending_stt_jobs/process_pending_translation_
jobs (docs/phase4-gemini-edar-extraction.md §Task execution), same reasoning as
Phase 3's split between STT and translation: each pipeline stage is independently
retryable and independently operable, and a Gemini-only backlog or degradation
shouldn't require touching the STT/translation cron entries, or vice versa.
"""

from django.core.management.base import BaseCommand

from csc_apps.processing.extraction_service import run_extraction_job
from csc_apps.processing.models import ProcessingJob


class Command(BaseCommand):
    help = 'Runs every PENDING/RETRYING EXTRACTION ProcessingJob against Gemini (docs/phase4-gemini-edar-extraction.md).'

    def handle(self, *args, **options):
        job_ids = list(
            ProcessingJob.objects.filter(job_type='EXTRACTION', status__in=['PENDING', 'RETRYING'])
            .order_by('created_at')
            .values_list('job_id', flat=True)
        )
        self.stdout.write(f'Found {len(job_ids)} runnable EXTRACTION job(s)')

        for job_id in job_ids:
            try:
                job = run_extraction_job(job_id)
            except Exception as e:  # noqa: BLE001 - one job's unexpected failure must not stop the batch
                self.stderr.write(f'job_id={job_id} raised unexpectedly: {e}')
                continue
            self.stdout.write(f'job_id={job_id} -> {job.status}')
