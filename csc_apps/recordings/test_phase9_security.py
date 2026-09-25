"""Phase 9 security/audit/observability regression tests
(docs/phase9-security-audit-observability.md). Does not re-test what Phase 2-8's
own test suites already cover (IDOR-per-endpoint, query counts, approval/export
content correctness) - this file targets the specific hardening/audit items Phase 9
adds or verifies structurally: identity spoofing, layer/provenance injection,
export/approval audit content, log/secret leakage, and error-message leakage.
"""

import tempfile
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from csc.config import Configurations
from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar import approval_service
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing.models import ProcessingJob
from csc_apps.processing.providers.base import ProviderError
from csc_apps.processing.stt_service import run_stt_job
from csc_apps.recordings.models.audio import Audio
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.storage import LocalPrivateAudioStorage
from csc_apps.recordings.tests import _TINY_VALID_WAV, _wav_file


class ConsolidatedIdorTests(TestCase):
    """One other-officer identity, checked against all four protected endpoints in
    one place - a single, explicit Phase 9 regression that all of them still deny
    access, rather than trusting each phase's own scattered per-endpoint test to
    never regress silently."""

    def setUp(self):
        self.owner = User.objects.create_user(email='idorowner@example.com', password='pw', name='O', role='OFFICER')
        self.other = User.objects.create_user(email='idorother@example.com', password='pw', name='X', role='OFFICER')
        self.recording = Recording.objects.create(officer=self.owner, status='READY_FOR_REVIEW', case_fir_number='FIR-IDOR')
        self.edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 1', confidence=0.9, extraction_version='v',
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.other)
        return client

    def test_other_officer_cannot_view_detail(self):
        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 400)

    def test_other_officer_recording_absent_from_own_history(self):
        response = self._client().get('/recordings/')
        ids = [r['recordingId'] for r in response.data['data']['data']]
        self.assertNotIn(self.recording.recording_id, ids)

    def test_other_officer_cannot_approve(self):
        response = self._client().post(f'/recordings/{self.recording.recording_id}/edar/approve/', {'fields': {}}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EdarFieldValue.objects.filter(layer='APPROVED').exists())

    def test_other_officer_cannot_export(self):
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.owner, edits={})
        response = self._client().get(f'/recordings/{self.recording.recording_id}/export/')
        self.assertEqual(response.status_code, 400)

    def test_filter_by_exact_case_fir_number_does_not_reveal_existence(self):
        response = self._client().get('/recordings/', {'case_fir_number': 'FIR-IDOR'})
        self.assertEqual(response.data['data']['data'], [])


class IdentitySpoofingTests(TestCase):
    """docs/phase9-security-audit-observability.md §5 - never trust a client-
    supplied identifier for ownership/actor attribution."""

    def setUp(self):
        self.officer = User.objects.create_user(email='spoof1@example.com', password='pw', name='O', role='OFFICER')
        self.victim = User.objects.create_user(email='spoof2@example.com', password='pw', name='V', role='OFFICER')

    def test_upload_officer_is_the_authenticated_user_not_a_request_field(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        # UploadRecordingRequestSerializer has no officer/user_id field at all - an
        # extra one in the body is simply ignored by DRF, never trusted.
        response = client.post(
            '/recordings/',
            {'audio': _wav_file(), 'officer_id': self.victim.user_id, 'user_id': self.victim.user_id},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        recording = Recording.objects.get(recording_id=response.data['data']['recordingId'])
        self.assertEqual(recording.officer_id, self.officer.user_id)

    def test_approving_officer_is_the_authenticated_user_not_a_request_field(self):
        recording = Recording.objects.create(officer=self.officer, status='READY_FOR_REVIEW')
        edar_record = EdarRecord.objects.create(recording=recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 1', confidence=0.9, extraction_version='v',
        )
        client = APIClient()
        client.force_authenticate(user=self.officer)
        response = client.post(
            f'/recordings/{recording.recording_id}/edar/approve/',
            {'fields': {}, 'officerId': self.victim.user_id, 'reviewedBy': self.victim.user_id},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        edar_record.refresh_from_db()
        self.assertEqual(edar_record.reviewed_by_id, self.officer.user_id)


class ApprovedLayerInjectionTests(TestCase):
    """docs/phase9-security-audit-observability.md §24 - the client cannot make the
    approval endpoint write anything but a validated {known, value} pair per
    field, regardless of what else is in the request body."""

    def setUp(self):
        self.officer = User.objects.create_user(email='inject1@example.com', password='pw', name='O', role='OFFICER')
        self.recording = Recording.objects.create(officer=self.officer, status='READY_FOR_REVIEW')
        self.edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 1', confidence=0.9, source_transcript_segment='ev', extraction_version='v1',
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        return client

    def test_extra_provenance_keys_in_a_field_edit_are_ignored(self):
        response = self._client().post(
            f'/recordings/{self.recording.recording_id}/edar/approve/',
            {
                'fields': {
                    'road_name': {
                        'known': 'KNOWN', 'value': 'NH 1, Ahmedabad',
                        'confidence': 0.99, 'evidence': 'fabricated evidence',
                        'extraction_version': 'fabricated-version', 'layer': 'APPROVED',
                    },
                },
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = EdarFieldValue.objects.get(edar_record=self.edar_record, field_key='road_name', layer='APPROVED')
        self.assertIsNone(row.confidence)
        self.assertIsNone(row.source_transcript_segment)
        self.assertIsNone(row.extraction_version)
        self.assertEqual(row.value, 'NH 1, Ahmedabad')

    def test_top_level_layer_field_in_request_body_is_ignored(self):
        response = self._client().post(
            f'/recordings/{self.recording.recording_id}/edar/approve/',
            {'fields': {}, 'layer': 'APPROVED'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        # Still exactly one AI + one APPROVED row - no second/duplicate/mislabeled
        # row was created from the stray top-level key.
        self.assertEqual(EdarFieldValue.objects.filter(edar_record=self.edar_record).count(), 2)

    def test_no_public_write_path_to_layer_approved_exists_outside_approval_service(self):
        # Structural check, not just behavioral: the only place in the whole
        # codebase that ever constructs an EdarFieldValue(..., layer='APPROVED')
        # is csc_apps.edar.approval_service. Every other file only ever *reads*
        # layer='APPROVED' (.filter(layer='APPROVED')), for GET/export.
        import glob
        import re

        pattern = re.compile(r"EdarFieldValue\([^)]*?layer\s*=\s*'APPROVED'", re.DOTALL)
        writers = [
            f for f in glob.glob('csc_apps/**/*.py', recursive=True)
            if '/test' not in f and not f.endswith('tests.py') and pattern.search(open(f).read())
        ]
        self.assertEqual(writers, ['csc_apps/edar/approval_service.py'])


class ExportAuditTests(TestCase):
    """docs/phase9-security-audit-observability.md §Export auditing (Phase 8
    deferred this)."""

    def setUp(self):
        self.officer = User.objects.create_user(email='exportaudit@example.com', password='pw', name='O', role='OFFICER')
        self.recording = Recording.objects.create(officer=self.officer, status='READY_FOR_REVIEW', case_fir_number='FIR-9')
        self.edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='SECRET-ROAD-NAME', confidence=0.9, extraction_version='v',
        )
        approval_service.approve_edar(edar_record=self.edar_record, officer=self.officer, edits={})

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        return client

    def test_successful_export_creates_activity_log_entry(self):
        self._client().get(f'/recordings/{self.recording.recording_id}/export/')
        log = ActivityLog.objects.filter(user=self.officer, action='Read', details__event='export').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.details['recording_id'], self.recording.recording_id)

    def test_export_audit_does_not_contain_edar_payload(self):
        self._client().get(f'/recordings/{self.recording.recording_id}/export/')
        log = ActivityLog.objects.get(user=self.officer, action='Read', details__event='export')
        rendered = str(log.details)
        self.assertNotIn('SECRET-ROAD-NAME', rendered)
        self.assertNotIn('road_name', rendered)

    def test_failed_export_attempt_does_not_create_audit_entry(self):
        other = User.objects.create_user(email='exportaudit2@example.com', password='pw', name='X', role='OFFICER')
        client = APIClient()
        client.force_authenticate(user=other)
        client.get(f'/recordings/{self.recording.recording_id}/export/')
        self.assertFalse(ActivityLog.objects.filter(details__event='export').exists())


class ErrorHandlingLeakageTests(TestCase):
    """docs/phase9-security-audit-observability.md §Error handling - an unexpected
    server exception must never reach the client as a raw traceback/internal
    detail, only through the existing {status, message, error} envelope."""

    def setUp(self):
        self.officer = User.objects.create_user(email='errleak@example.com', password='pw', name='O', role='OFFICER')
        self.recording = Recording.objects.create(officer=self.officer, status='PROCESSING')

    def test_unexpected_exception_with_debug_false_returns_generic_message(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        sensitive = 'RuntimeError: connection to internal-db-host:5432 refused, password=hunter2'
        with patch.object(Configurations, 'debug', False):
            with patch('csc_apps.recordings.views.Recording.objects') as mocked:
                mocked.select_related.side_effect = RuntimeError(sensitive)
                response = client.get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 400)
        rendered = str(response.data)
        self.assertNotIn('hunter2', rendered)
        self.assertNotIn('internal-db-host', rendered)
        self.assertNotIn('Traceback', rendered)

    def test_unexpected_exception_with_debug_true_still_has_no_python_traceback_object(self):
        # DEBUG=True intentionally surfaces more diagnostic text (Utils.
        # env_exception_handler) for local development, but never a raw traceback -
        # only the exception's own string form, through the same enveloped
        # response.
        client = APIClient()
        client.force_authenticate(user=self.officer)
        with patch.object(Configurations, 'debug', True):
            with patch('csc_apps.recordings.views.Recording.objects') as mocked:
                mocked.select_related.side_effect = RuntimeError('boom')
                response = client.get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback (most recent call last)', str(response.data))


class UploadPathSecurityTests(TestCase):
    """docs/phase9-security-audit-observability.md §Upload security - end-to-end
    through the real HTTP upload path, not just the validator unit
    (AudioUploadValidatorTests already covers the validator directly)."""

    def setUp(self):
        self.officer = User.objects.create_user(email='pathsec@example.com', password='pw', name='O', role='OFFICER')

    def test_path_traversal_filename_does_not_escape_storage_root(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        response = client.post(
            '/recordings/',
            {'audio': _wav_file(name='../../../../etc/passwd.wav')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        audio = Audio.objects.get(audio_id=response.data['data']['audioId'])
        self.assertNotIn('..', audio.storage_path)
        self.assertNotIn('etc/passwd', audio.storage_path)
        self.assertTrue(audio.storage_path.startswith(f'recordings/{audio.recording_id}/audio/'))

    def test_storage_key_is_server_generated_not_the_original_filename(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        response = client.post('/recordings/', {'audio': _wav_file(name='evidence-statement.wav')}, format='multipart')
        audio = Audio.objects.get(audio_id=response.data['data']['audioId'])
        self.assertNotIn('evidence-statement', audio.storage_path)


class LoggingSecurityTests(TestCase):
    """docs/phase9-security-audit-observability.md §Logging - proves, not just
    asserts, that the structured processing-service log lines never interpolate a
    raw provider error message/secret, only controlled fields (job_id, error_code,
    status, duration)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.officer = User.objects.create_user(email='logsec@example.com', password='pw', name='O', role='OFFICER')
        self.recording = Recording.objects.create(officer=self.officer, status='UPLOADED')
        storage = LocalPrivateAudioStorage()
        stored = storage.save(
            recording_id=self.recording.recording_id, extension='.wav',
            fileobj=SimpleUploadedFile('statement.wav', _TINY_VALID_WAV, content_type='audio/wav'),
        )
        audio = Audio.objects.create(
            recording=self.recording, source='UPLOAD', storage_path=stored.storage_path,
            content_type='audio/wav', file_size_bytes=stored.size_bytes, checksum_sha256=stored.checksum_sha256,
        )
        self.job = ProcessingJob.objects.create(
            recording=self.recording, job_type='STT', audio=audio, status='PENDING'
        )

    def test_provider_error_message_never_reaches_the_structured_log_line(self):
        fake_secret = 'Authorization: Bearer sk_live_should_never_be_logged_12345'
        provider = MagicMock()
        provider.transcribe.side_effect = ProviderError('STT_PROVIDER_UNAVAILABLE', fake_secret)

        with self.assertLogs('csc_apps.processing.stt_service', level='INFO') as captured:
            run_stt_job(self.job.job_id, provider=provider)

        rendered = '\n'.join(captured.output)
        self.assertNotIn(fake_secret, rendered)
        self.assertNotIn('sk_live_should_never_be_logged', rendered)
        self.assertIn('STT_PROVIDER_UNAVAILABLE', rendered)

    def test_no_logger_call_interpolates_a_raw_provider_error_message(self):
        # Structural check across all three provider services: every logger.*()
        # call site passes only controlled fields (job_id, recording_id,
        # error_code, status, duration, metrics) - never `error`, `str(error)`, or
        # `.message`/`.body` from a caught ProviderError/SDK exception. This is
        # what makes the assertLogs test above a general guarantee rather than one
        # provider/module's happy accident.
        import ast
        import glob

        offending = []
        for path in glob.glob('csc_apps/processing/*.py'):
            tree = ast.parse(open(path).read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_logger_call = (
                    isinstance(func, ast.Attribute) and func.attr in ('info', 'warning', 'error', 'exception')
                    and isinstance(func.value, ast.Name) and func.value.id == 'logger'
                )
                if not is_logger_call:
                    continue
                for arg in node.args[1:]:
                    src = ast.unparse(arg)
                    # `error.error_code`/`e.error_code` is the one safe, controlled
                    # exception attribute this codebase logs - strip it before
                    # checking for anything else exception-shaped, which would be
                    # the raw message/body.
                    stripped = src.replace('error.error_code', '').replace('e.error_code', '')
                    if '.message' in stripped or '.body' in stripped or 'str(e' in stripped or stripped.strip() in ('error', 'e'):
                        offending.append(f'{path}:{node.lineno}: {src}')
        self.assertEqual(offending, [])
