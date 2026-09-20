from unittest.mock import MagicMock

from django.test import TestCase
from rest_framework.test import APIClient

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing import event_types
from csc_apps.processing.extraction_service import run_extraction_job
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ExtractedField, ExtractionResult, FieldSource
from csc_apps.recordings.models.audio import Transcript
from csc_apps.recordings.models.recording import Recording

ENGLISH = 'The motorcycle hit the rear of the car at the intersection.'
VERSION = 'gemini-3.8-flash/prompt-v1/schema-0.1.0'


def field(key, value, confidence=0.9, evidence='hit the rear of the car'):
    return ExtractedField(field=key, value=value, confidence=confidence, source=FieldSource(transcript_segment=evidence))


def result(*fields, vehicles=0, casualties=0):
    return ExtractionResult(
        fields=list(fields), provider_name='gemini', extraction_version=VERSION,
        provider_metadata={'vehicle_count': vehicles, 'casualty_count': casualties},
    )


def provider_returning(res):
    p = MagicMock()
    p.extract.return_value = res
    return p


class Phase5Base(TestCase):
    def setUp(self):
        self.officer = User.objects.create_user(email='o5@example.com', password='pw', name='O', role='OFFICER')
        self.other = User.objects.create_user(email='x5@example.com', password='pw', name='X', role='OFFICER')
        self.reviewer = User.objects.create_user(email='r5@example.com', password='pw', name='R', role='REVIEWER')
        self.recording = Recording.objects.create(officer=self.officer, status='PROCESSING')
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='original text',
            detected_language_code='hi-IN', provider_name='sarvam',
        )
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='SUCCEEDED')
        self.english = Transcript.objects.create(
            recording=self.recording, language='ENGLISH', text=ENGLISH,
            detected_language_code='en-IN', provider_name='sarvam',
        )
        self.job = ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')

    def run_job(self, res, job=None):
        job = job or self.job
        run_extraction_job(job.job_id, provider=provider_returning(res))
        job.refresh_from_db()
        return job

    def get(self, user):
        client = APIClient()
        if user:
            client.force_authenticate(user=user)
        return client.get(f'/recordings/{self.recording.recording_id}/')


class ProvenancePersistenceTests(Phase5Base):
    def test_success_links_record_to_transcript_job_and_version(self):
        self.run_job(result(field('crash_type', 'rear-end collision')))
        record = EdarRecord.objects.get(recording=self.recording)
        self.assertEqual(record.source_transcript_id, self.english.pk)
        self.assertEqual(record.extraction_job_id, self.job.job_id)
        self.assertIsNotNone(record.extracted_at)
        self.assertEqual(record.quality_status, 'VALIDATED')
        self.assertEqual(record.quality_report['status'], 'VALIDATED')
        row = EdarFieldValue.objects.get(edar_record=record, field_key='crash_type')
        self.assertEqual(row.extraction_version, VERSION)
        self.assertEqual(row.source_transcript_segment, 'hit the rear of the car')
        self.assertEqual(row.confidence, 0.9)

    def test_unknown_fields_are_unknown_without_provenance(self):
        self.run_job(result(field('crash_type', 'x')))
        row = EdarFieldValue.objects.get(field_key='road_name', layer='AI')
        self.assertEqual(row.known, 'UNKNOWN')
        self.assertIsNone(row.value)
        self.assertIsNone(row.confidence)
        self.assertIsNone(row.source_transcript_segment)

    def test_english_transcript_is_not_mutated(self):
        self.run_job(result(field('crash_type', 'x')))
        self.english.refresh_from_db()
        self.assertEqual(self.english.text, ENGLISH)

    def test_evidence_matching_another_recordings_transcript_is_flagged(self):
        other = Recording.objects.create(officer=self.officer, status='PROCESSING')
        Transcript.objects.create(
            recording=other, language='ENGLISH', text='A pedestrian was struck by a bus.',
            detected_language_code='en-IN', provider_name='sarvam',
        )
        self.run_job(result(field('crash_type', 'x', evidence='A pedestrian was struck by a bus')))
        record = EdarRecord.objects.get(recording=self.recording)
        self.assertEqual(record.quality_status, 'VALIDATION_WARNING')
        self.assertEqual(record.quality_report['warnings'][0]['code'], 'evidence_not_traceable')

    def test_warning_status_persisted_and_events_written(self):
        job = self.run_job(result(field('crash_type', 'x', confidence=0.2)))
        self.assertEqual(job.status, 'SUCCEEDED')
        self.assertEqual(EdarRecord.objects.get().quality_status, 'VALIDATION_WARNING')
        types = set(ProcessingEvent.objects.filter(recording=self.recording).values_list('event_type', flat=True))
        self.assertIn(event_types.QUALITY_VALIDATION_SUCCEEDED, types)
        self.assertIn(event_types.EXTRACTION_SUCCEEDED, types)

    def test_activity_log_has_no_evidence_or_values(self):
        self.run_job(result(field('crash_type', 'TOPSECRETVALUE', evidence='hit the rear of the car')))
        rendered = ' '.join(str(a.__dict__) for a in ActivityLog.objects.all())
        self.assertNotIn('TOPSECRETVALUE', rendered)
        self.assertNotIn('hit the rear', rendered)


class NoFalseApprovalTests(Phase5Base):
    def test_extraction_never_writes_approved_layer(self):
        self.run_job(result(field('crash_type', 'x')))
        self.assertFalse(EdarFieldValue.objects.filter(layer='APPROVED').exists())
        self.assertTrue(EdarFieldValue.objects.filter(layer='AI').exists())
        self.recording.refresh_from_db()
        self.assertNotIn(self.recording.status, ('APPROVED', 'COMPLETED'))

    def test_invalid_candidate_writes_nothing_approved_either(self):
        self.run_job(result(field('crash_type', 'x', confidence=3)))
        self.assertFalse(EdarFieldValue.objects.exists())


class InvalidCandidateTests(Phase5Base):
    def test_invalid_candidate_fails_job_with_report_and_no_rows(self):
        job = self.run_job(result(field('crash_type', 'x', confidence=1.5)))
        self.assertEqual(job.status, 'FAILED')
        self.assertEqual(job.error_code, 'EXTRACTION_SCHEMA_VALIDATION_FAILED')
        self.assertEqual(job.provider_metadata['validation_report']['status'], 'INVALID')
        self.assertFalse(EdarFieldValue.objects.exists())
        types = set(ProcessingEvent.objects.filter(recording=self.recording).values_list('event_type', flat=True))
        self.assertIn(event_types.QUALITY_VALIDATION_FAILED, types)
        self.assertNotIn('1.5', job.error_message)

    def test_failed_validation_preserves_previous_valid_candidate(self):
        self.run_job(result(field('crash_type', 'first')))
        before = list(EdarFieldValue.objects.filter(layer='AI').order_by('field_key').values_list('field_key', 'value'))
        record_before = EdarRecord.objects.get()

        second = ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')
        job = self.run_job(result(field('crash_type', 'second', confidence=-1)), job=second)
        self.assertEqual(job.status, 'FAILED')

        after = list(EdarFieldValue.objects.filter(layer='AI').order_by('field_key').values_list('field_key', 'value'))
        self.assertEqual(before, after)
        record_after = EdarRecord.objects.get()
        self.assertEqual(record_after.extraction_job_id, record_before.extraction_job_id)
        self.assertEqual(record_after.quality_status, record_before.quality_status)

    def test_successful_reprocess_replaces_previous_candidate(self):
        self.run_job(result(field('crash_type', 'first')))
        second = ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')
        self.run_job(result(field('crash_type', 'second')), job=second)
        self.assertEqual(EdarFieldValue.objects.get(field_key='crash_type', layer='AI').value, 'second')
        self.assertEqual(EdarRecord.objects.get().extraction_job_id, second.job_id)
        self.assertEqual(EdarRecord.objects.count(), 1)


class RecordingDetailQualityAPITests(Phase5Base):
    def test_get_returns_quality_provenance_evidence_and_confidence(self):
        self.run_job(result(
            field('crash_type', 'rear-end collision'),
            field('road_type', 'urban', confidence=0.3, evidence='not in transcript at all'),
        ))
        response = self.get(self.officer)
        self.assertEqual(response.status_code, 200, response.data)
        edar = response.data['data']['edar']
        self.assertEqual(edar['layer'], 'AI')
        self.assertEqual(edar['quality']['status'], 'VALIDATION_WARNING')
        self.assertEqual(edar['provenance']['extractionVersion'], VERSION)
        self.assertEqual(edar['provenance']['sourceTranscriptLanguage'], 'ENGLISH')
        good = edar['fields']['crash_type']
        self.assertEqual((good['confidence'], good['evidence'], good['evidenceVerified']), (0.9, 'hit the rear of the car', True))
        bad = edar['fields']['road_type']
        self.assertFalse(bad['evidenceVerified'])
        self.assertEqual(set(bad['warnings']), {'evidence_not_traceable', 'low_confidence'})
        unknown = edar['fields']['road_name']
        self.assertEqual(unknown['known'], 'UNKNOWN')
        self.assertIsNone(unknown['evidence'])
        self.assertIsNone(unknown['evidenceVerified'])

    def test_metrics_have_no_accuracy_score(self):
        self.run_job(result(field('crash_type', 'x')))
        metrics = self.get(self.officer).data['data']['edar']['quality']['metrics']
        self.assertEqual(metrics['knownFields'], 1)
        self.assertFalse([k for k in metrics if 'accuracy' in k.lower()])

    def test_get_does_not_validate_or_call_providers(self):
        self.run_job(result(field('crash_type', 'x')))
        count = ProcessingEvent.objects.count()
        jobs = ProcessingJob.objects.count()
        self.get(self.officer)
        self.assertEqual((ProcessingEvent.objects.count(), ProcessingJob.objects.count()), (count, jobs))

    def test_failed_validation_exposes_structured_issues_and_no_edar(self):
        self.run_job(result(field('crash_type', 'SECRETVAL', confidence=7)))
        data = self.get(self.officer).data['data']
        self.assertIsNone(data['edar'])
        self.assertEqual(data['extractionStatus'], 'FAILED')
        self.assertEqual(data['extractionFailureReason'], 'EXTRACTION_SCHEMA_VALIDATION_FAILED')
        self.assertEqual(data['extractionIssues'][0]['code'], 'invalid_confidence')
        self.assertEqual(data['extractionIssues'][0]['field'], 'crash_type')
        self.assertNotIn('SECRETVAL', str(data))

    def test_failed_later_extraction_still_shows_previous_candidate_status_from_latest_job(self):
        self.run_job(result(field('crash_type', 'first')))
        second = ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')
        self.run_job(result(field('crash_type', 'second', confidence=-1)), job=second)
        data = self.get(self.officer).data['data']
        self.assertEqual(data['extractionStatus'], 'FAILED')
        self.assertTrue(EdarFieldValue.objects.filter(field_key='crash_type', value='first').exists())

    def test_no_provider_internals_exposed(self):
        self.run_job(result(field('crash_type', 'x')))
        rendered = str(self.get(self.officer).data)
        for forbidden in ('provider_metadata', 'validation_report', 'api_key', 'request_id'):
            self.assertNotIn(forbidden, rendered)

    def test_authorization_scenarios(self):
        self.run_job(result(field('crash_type', 'x')))
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.get(self.officer).status_code, 200)
        self.assertEqual(self.get(self.reviewer).status_code, 200)
        other = self.get(self.other)
        self.assertEqual(other.status_code, 400)
        self.assertNotIn('edar', str(other.data.get('data')))

    def test_endpoint_is_read_only(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        for method in ('put', 'patch', 'delete'):
            response = getattr(client, method)(f'/recordings/{self.recording.recording_id}/', {})
            self.assertEqual(response.status_code, 405, method)
