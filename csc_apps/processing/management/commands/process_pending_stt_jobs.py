"""Runs every runnable STT ProcessingJob to completion or failure.

docs/phase2-sarvam-stt.md §Task execution: PMS has no background-worker precedent
(docs/pms-reference-analysis.md §1) and Phase 0 deliberately deferred a concrete
queue technology (ADR-009, docs/open-decisions.md OD-003). Rather than introduce
Celery/RQ/Dramatiq for a single job type, this management command is the "clean
mechanism to execute an STT ProcessingJob" the source instructions ask for: it can be
invoked by cron, by a systemd timer, or by hand - anything that can run
`python manage.py process_pending_stt_jobs` on an interval. It is intentionally never
called from the Phase 1 upload request (docs/phase1-audio-ingestion.md §15 - the
upload endpoint must not block on external AI processing).
"""

from django.core.management.base import BaseCommand

from csc_apps.processing.models import ProcessingJob
from csc_apps.processing.stt_service import run_stt_job


class Command(BaseCommand):
    help = 'Runs every PENDING/RETRYING STT ProcessingJob against Sarvam (docs/phase2-sarvam-stt.md).'

    def handle(self, *args, **options):
        job_ids = list(
            ProcessingJob.objects.filter(job_type='STT', status__in=['PENDING', 'RETRYING'])
            .order_by('created_at')
            .values_list('job_id', flat=True)
        )
        self.stdout.write(f'Found {len(job_ids)} runnable STT job(s)')

        for job_id in job_ids:
            try:
                job = run_stt_job(job_id)
            except Exception as e:  # noqa: BLE001 - one job's unexpected failure must not stop the batch
                self.stderr.write(f'job_id={job_id} raised unexpectedly: {e}')
                continue
            self.stdout.write(f'job_id={job_id} -> {job.status}')
