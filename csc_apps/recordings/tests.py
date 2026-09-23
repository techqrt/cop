import datetime as dt
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar import approval_service
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing import event_types
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.recordings.models.audio import Audio, Transcript
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.state_machine import can_transition, transition
from csc_apps.recordings.storage import AudioStorageError, LocalPrivateAudioStorage
from csc_apps.recordings.validators import validate_audio_upload

_FIXTURES_DIR = Path(__file__).resolve().parent / 'tests_fixtures'
_TINY_VALID_WAV = (_FIXTURES_DIR / 'tiny_valid.wav').read_bytes()


class RecordingStateMachineTests(TestCase):
    """docs/recording-state-machine.md §3 - only the documented transitions are legal,
    and every transition is recorded as a ProcessingEvent (docs/observability.md)."""

    def setUp(self):
        officer = User.objects.create_user(
            email='officer@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=officer)

    def test_created_can_move_to_recording_or_uploaded(self):
        self.assertTrue(can_transition('CREATED', 'RECORDING'))
        self.assertTrue(can_transition('CREATED', 'UPLOADED'))

    def test_completed_is_terminal(self):
        self.assertFalse(can_transition('COMPLETED', 'IN_REVIEW'))
        self.assertEqual(can_transition('COMPLETED', 'COMPLETED'), False)

    def test_illegal_transition_raises_value_error(self):
        with self.assertRaises(ValueError):
            transition(self.recording, 'COMPLETED')

    def test_legal_transition_updates_status_and_writes_event(self):
        transition(self.recording, 'UPLOADED')
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'UPLOADED')

        event = ProcessingEvent.objects.get(recording=self.recording)
        self.assertEqual(event.metadata['from_state'], 'CREATED')
        self.assertEqual(event.metadata['to_state'], 'UPLOADED')

    def test_failure_and_retry_path(self):
        transition(self.recording, 'UPLOADED')
        transition(self.recording, 'PROCESSING')
        transition(self.recording, 'FAILED')
        transition(self.recording, 'RETRY')
        transition(self.recording, 'PROCESSING')
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'PROCESSING')


def _wav_file(name='statement.wav', content=_TINY_VALID_WAV, content_type='audio/wav'):
    return SimpleUploadedFile(name=name, content=content, content_type=content_type)


class AudioUploadValidatorTests(SimpleTestCase):
    """docs/phase1-audio-ingestion.md §Validation - each rule is checked
    independently of the HTTP layer, and never trusts the declared content-type
    alone."""

    def test_valid_wav_passes(self):
        validated = validate_audio_upload(_wav_file())
        self.assertEqual(validated.extension, '.wav')
        self.assertEqual(validated.original_filename, 'statement.wav')

    def test_empty_file_rejected(self):
        with self.assertRaises(ValueError):
            validate_audio_upload(_wav_file(content=b''))

    @override_settings(AUDIO_MAX_UPLOAD_SIZE_BYTES=10)
    def test_oversized_file_rejected(self):
        with self.assertRaises(ValueError):
            validate_audio_upload(_wav_file())

    def test_unsupported_content_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_audio_upload(_wav_file(name='doc.txt', content=b'hello', content_type='text/plain'))

    def test_content_signature_mismatch_is_rejected(self):
        # Claims audio/wav but is not RIFF/WAVE - must not be trusted on
        # content-type alone (docs/phase1-audio-ingestion.md §Validation).
        with self.assertRaises(ValueError):
            validate_audio_upload(_wav_file(content=b'not actually a wav file' * 4, content_type='audio/wav'))

    def test_mp3_id3_signature_accepted(self):
        content = b'ID3' + b'\x00' * 30
        validated = validate_audio_upload(_wav_file(name='x.mp3', content=content, content_type='audio/mpeg'))
        self.assertEqual(validated.extension, '.mp3')

    def test_original_filename_is_sanitized_to_basename(self):
        validated = validate_audio_upload(_wav_file(name='../../etc/passwd.wav'))
        self.assertEqual(validated.original_filename, 'passwd.wav')


class LocalPrivateAudioStorageTests(SimpleTestCase):
    """docs/phase1-audio-ingestion.md §Storage architecture - private local backend,
    server-generated storage keys, no in-place mutation, integrity via checksum."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.storage = LocalPrivateAudioStorage()

    def test_save_computes_correct_checksum(self):
        stored = self.storage.save(recording_id=1, extension='.wav', fileobj=_wav_file())
        self.assertEqual(stored.checksum_sha256, hashlib.sha256(_TINY_VALID_WAV).hexdigest())
        self.assertEqual(stored.size_bytes, len(_TINY_VALID_WAV))

    def test_save_path_is_scoped_by_recording_id_and_not_original_filename(self):
        stored = self.storage.save(recording_id=42, extension='.wav', fileobj=_wav_file(name='evidence.wav'))
        self.assertTrue(stored.storage_path.startswith('recordings/42/audio/'))
        self.assertNotIn('evidence', stored.storage_path)

    def test_two_saves_for_same_recording_do_not_collide(self):
        first = self.storage.save(recording_id=7, extension='.wav', fileobj=_wav_file())
        second = self.storage.save(recording_id=7, extension='.wav', fileobj=_wav_file())
        self.assertNotEqual(first.storage_path, second.storage_path)
        self.assertTrue(Path(self._tmp.name, first.storage_path).exists())
        self.assertTrue(Path(self._tmp.name, second.storage_path).exists())

    def test_delete_removes_file(self):
        stored = self.storage.save(recording_id=1, extension='.wav', fileobj=_wav_file())
        self.assertTrue(Path(self._tmp.name, stored.storage_path).exists())
        self.storage.delete(stored.storage_path)
        self.assertFalse(Path(self._tmp.name, stored.storage_path).exists())

    def test_open_returns_stored_bytes_unchanged(self):
        stored = self.storage.save(recording_id=1, extension='.wav', fileobj=_wav_file())
        with self.storage.open(stored.storage_path) as f:
            self.assertEqual(f.read(), _TINY_VALID_WAV)

    def test_failed_write_leaves_no_partial_file(self):
        class ExplodingFile:
            name = 'boom.wav'

            def read(self, _size):
                raise OSError('simulated disk failure')

        with self.assertRaises(AudioStorageError):
            self.storage.save(recording_id=9, extension='.wav', fileobj=ExplodingFile())

        leftovers = list(Path(self._tmp.name, 'recordings', '9', 'audio').glob('*')) if Path(
            self._tmp.name, 'recordings', '9', 'audio'
        ).exists() else []
        self.assertEqual(leftovers, [])


class RecordingUploadAPITests(TestCase):
    """End-to-end ingestion flow (docs/phase1-audio-ingestion.md). Storage is
    redirected to a temp directory per test so nothing is written under the repo's
    real private_storage/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.officer = User.objects.create_user(
            email='officer@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin@example.com', password='pw', name='Admin One', role='ADMIN'
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _upload(self, client, **extra_fields):
        payload = {'audio': _wav_file(), **extra_fields}
        return client.post('/recordings/', payload, format='multipart')

    def test_authenticated_upload_succeeds_and_creates_all_records(self):
        response = self._upload(
            self._client_as(self.officer),
            gps_latitude='26.14',
            gps_longitude='91.73',
            road_name='NH-37 near Khanapara',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['status'])
        data = response.data['data']

        recording = Recording.objects.get(recording_id=data['recordingId'])
        audio = Audio.objects.get(audio_id=data['audioId'])
        job = ProcessingJob.objects.get(job_id=data['processingJobId'])

        self.assertEqual(recording.officer_id, self.officer.user_id)
        self.assertEqual(recording.road_name, 'NH-37 near Khanapara')
        self.assertEqual(audio.recording_id, recording.recording_id)
        self.assertEqual(audio.source, 'UPLOAD')
        self.assertEqual(audio.checksum_sha256, hashlib.sha256(_TINY_VALID_WAV).hexdigest())
        self.assertEqual(audio.file_size_bytes, len(_TINY_VALID_WAV))
        self.assertEqual(job.recording_id, recording.recording_id)
        self.assertEqual(job.job_type, 'STT')
        self.assertEqual(job.status, 'PENDING')

    def test_upload_above_djangos_default_in_memory_threshold_succeeds(self):
        # Regression: Django's own default per-file in-memory threshold is 2.5MB;
        # above it, an uploaded file becomes an on-disk TemporaryUploadedFile
        # wrapping a real OS file handle. csc_apps.common.serializer_validations
        # deep-copies request.data to get a mutable QueryDict (`data.copy()`), and
        # deepcopy cannot copy that handle - every real crash-statement recording
        # over ~80 seconds of 16kHz WAV hit an unhandled 500 instead of the shared
        # {status,message,data} envelope (fixed by raising
        # FILE_UPLOAD_MAX_MEMORY_SIZE in csc/settings.py).
        large_content = b'RIFF' + b'\x00\x00\x00\x00' + b'WAVE' + b'\x00' * (3 * 1024 * 1024)
        payload = {'audio': _wav_file(content=large_content)}
        response = self._client_as(self.officer).post('/recordings/', payload, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['status'])

    def test_unauthenticated_upload_is_rejected(self):
        response = self._upload(APIClient())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Recording.objects.count(), 0)

    def test_any_authenticated_role_can_upload(self):
        # docs/open-decisions.md OD-007 - the role permission matrix is still open;
        # Phase 1's interim default is that every authenticated role may ingest audio.
        for user in (self.officer, self.reviewer, self.admin):
            response = self._upload(self._client_as(user))
            self.assertEqual(response.status_code, 201, (user.role, response.data))

    def test_missing_audio_file_is_rejected_and_creates_nothing(self):
        response = self._client_as(self.officer).post('/recordings/', {}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Recording.objects.count(), 0)
        self.assertEqual(ProcessingJob.objects.count(), 0)

    def test_unsupported_format_is_rejected_and_creates_nothing(self):
        client = self._client_as(self.officer)
        response = client.post(
            '/recordings/',
            {'audio': SimpleUploadedFile('note.txt', b'not audio', content_type='text/plain')},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])
        self.assertEqual(Recording.objects.count(), 0)
        self.assertEqual(ProcessingJob.objects.count(), 0)

    def test_oversized_file_is_rejected_and_creates_nothing(self):
        with override_settings(AUDIO_MAX_UPLOAD_SIZE_BYTES=10):
            response = self._upload(self._client_as(self.officer))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Recording.objects.count(), 0)

    def test_success_response_follows_pms_envelope(self):
        response = self._upload(self._client_as(self.officer))
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})
        self.assertTrue(response.data['status'])

    def test_failure_response_follows_pms_envelope(self):
        response = self._client_as(self.officer).post('/recordings/', {}, format='multipart')
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'error'})
        self.assertFalse(response.data['status'])

    def test_recording_reaches_processing_state_not_ready_for_review(self):
        response = self._upload(self._client_as(self.officer))
        recording = Recording.objects.get(recording_id=response.data['data']['recordingId'])
        self.assertEqual(recording.status, 'PROCESSING')

    def test_processing_events_recorded_for_ingestion_milestones(self):
        response = self._upload(self._client_as(self.officer))
        recording_id = response.data['data']['recordingId']
        event_types_recorded = list(
            ProcessingEvent.objects.filter(recording_id=recording_id).order_by('occurred_at').values_list(
                'event_type', flat=True
            )
        )
        for expected in (
            event_types.RECORDING_CREATED,
            event_types.AUDIO_VALIDATED,
            event_types.AUDIO_STORED,
            event_types.PROCESSING_JOB_CREATED,
        ):
            self.assertIn(expected, event_types_recorded)
        # State transitions also log their own generic event
        # (csc_apps.recordings.state_machine.transition).
        self.assertIn('recording_transitioned_created_to_uploaded', event_types_recorded)
        self.assertIn('recording_transitioned_uploaded_to_processing', event_types_recorded)

    def test_activity_log_is_created(self):
        response = self._upload(self._client_as(self.officer))
        recording_id = response.data['data']['recordingId']
        log = ActivityLog.objects.get(user=self.officer, model='Recording')
        self.assertEqual(log.action, 'Create')
        self.assertEqual(log.details['recording_id'], recording_id)

    def test_stored_audio_path_is_outside_media_and_static_roots(self):
        response = self._upload(self._client_as(self.officer))
        audio = Audio.objects.get(audio_id=response.data['data']['audioId'])
        absolute_path = Path(self._tmp.name) / audio.storage_path
        self.assertTrue(absolute_path.exists())
        self.assertNotIn(str(settings.MEDIA_ROOT), str(absolute_path))
        # No response field exposes a servable URL for the audio.
        self.assertNotIn('audioUrl', response.data['data'])
        self.assertNotIn('storagePath', response.data['data'])

    def test_storage_failure_creates_no_database_records(self):
        with patch('csc_apps.recordings.views.get_storage') as mock_get_storage:
            mock_get_storage.return_value.save.side_effect = AudioStorageError('disk full')
            response = self._upload(self._client_as(self.officer))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])
        self.assertEqual(Recording.objects.count(), 0)
        self.assertEqual(Audio.objects.count(), 0)
        self.assertEqual(ProcessingJob.objects.count(), 0)

    def test_database_failure_after_storage_success_deletes_stored_file_and_rolls_back(self):
        with patch('csc_apps.recordings.views.Audio.objects.create', side_effect=RuntimeError('db down')):
            response = self._upload(self._client_as(self.officer))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Recording.objects.count(), 0)
        self.assertEqual(Audio.objects.count(), 0)
        # The file storage.save() wrote before the simulated DB failure must have
        # been compensated away - nothing left under the temp storage root.
        leftover_files = [p for p in Path(self._tmp.name).rglob('*') if p.is_file()]
        self.assertEqual(leftover_files, [])

    def test_two_sequential_uploads_do_not_overwrite_or_corrupt_first_file(self):
        client = self._client_as(self.officer)
        first = self._upload(client)
        second = self._upload(client)

        first_audio = Audio.objects.get(audio_id=first.data['data']['audioId'])
        second_audio = Audio.objects.get(audio_id=second.data['data']['audioId'])

        self.assertNotEqual(first_audio.storage_path, second_audio.storage_path)
        first_bytes = (Path(self._tmp.name) / first_audio.storage_path).read_bytes()
        self.assertEqual(first_bytes, _TINY_VALID_WAV)

    def test_duplicate_checksum_within_window_is_flagged(self):
        client = self._client_as(self.officer)
        first = self._upload(client)
        second = self._upload(client)

        self.assertIsNone(first.data['data']['possibleDuplicateOfRecordingId'])
        self.assertEqual(
            second.data['data']['possibleDuplicateOfRecordingId'], first.data['data']['recordingId']
        )

    def test_duplicate_checksum_outside_window_is_not_flagged(self):
        client = self._client_as(self.officer)
        first = self._upload(client)
        Audio.objects.filter(audio_id=first.data['data']['audioId']).update(
            uploaded_at=timezone.now() - dt.timedelta(minutes=30)
        )

        second = self._upload(client)
        self.assertIsNone(second.data['data']['possibleDuplicateOfRecordingId'])


class RecordingDetailAPITests(TestCase):
    """GET /recordings/<id>/ (docs/phase2-sarvam-stt.md §Authorization, §Testing D).
    Covers the exact five scenarios the source instructions call out in §23:
    unauthenticated -> rejected; authenticated+authorized -> allowed;
    authenticated+unauthorized -> rejected; owner's own recording -> allowed;
    another user's recording -> inaccessible."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='other@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin@example.com', password='pw', name='Admin One', role='ADMIN'
        )
        self.recording = Recording.objects.create(officer=self.owner, status='PROCESSING')

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get(self, user=None):
        client = self._client_as(user) if user else APIClient()
        return client.get(f'/recordings/{self.recording.recording_id}/')

    def test_unauthenticated_request_is_rejected(self):
        response = self._get()
        self.assertEqual(response.status_code, 401)

    def test_owner_can_access_own_recording(self):
        response = self._get(self.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['recordingId'], self.recording.recording_id)

    def test_other_officer_cannot_access_someone_elses_recording(self):
        response = self._get(self.other_officer)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])

    def test_reviewer_can_access_any_recording(self):
        response = self._get(self.reviewer)
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_can_access_any_recording(self):
        response = self._get(self.admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_nonexistent_recording_id_is_rejected(self):
        response = self._client_as(self.owner).get('/recordings/999999/')
        self.assertEqual(response.status_code, 400)

    def test_pending_status_returned_without_fabricating_a_transcript(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='PENDING')
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'PENDING')
        self.assertIsNone(data['transcript']['original'])
        self.assertIsNone(data['transcript']['english'])
        self.assertIsNone(data['translationStatus'])

    def test_running_status_returned_without_fabricating_a_transcript(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='RUNNING')
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'RUNNING')
        self.assertIsNone(data['transcript']['original'])

    def test_failed_status_returns_error_code_not_raw_message(self):
        ProcessingJob.objects.create(
            recording=self.recording, job_type='STT', status='FAILED',
            error_code='STT_UNSUPPORTED_INPUT', error_message='Sarvam API error: {"secret": "internal detail"}',
        )
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'FAILED')
        self.assertEqual(data['failureReason'], 'STT_UNSUPPORTED_INPUT')
        self.assertIsNone(data['transcript']['original'])
        self.assertNotIn('secret', str(response.data))

    def test_succeeded_status_returns_original_language_transcript(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='namaste, gaadi accident ho gaya',
            detected_language_code='hi-IN', provider_name='sarvam',
            provider_metadata={'model': 'saaras:v3', 'request_id': 'req-1'},
        )
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'SUCCEEDED')
        self.assertEqual(data['transcript']['original']['text'], 'namaste, gaadi accident ho gaya')
        self.assertEqual(data['transcript']['original']['language'], 'hi-IN')
        self.assertIsNone(data['transcript']['english'])

    def test_raw_sarvam_response_fields_are_not_exposed(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='hello',
            detected_language_code='hi-IN', provider_name='sarvam',
            provider_metadata={'model': 'saaras:v3', 'request_id': 'req-should-not-leak'},
        )
        response = self._get(self.owner)
        self.assertNotIn('request_id', str(response.data))
        self.assertNotIn('providerMetadata', response.data['data']['transcript'])
        self.assertNotIn('provider_name', str(response.data))

    def test_response_follows_pms_envelope(self):
        response = self._get(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})
        self.assertTrue(response.data['status'])


class RecordingDetailTranslationAPITests(TestCase):
    """GET /recordings/<id>/ - the Phase 3 extension (docs/phase3-sarvam-translation.md
    §API response, §Testing D). processingStatus keeps its exact Phase 2 meaning
    (STT-only); translationStatus/transcript.english are additive."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner3@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=self.owner, status='PROCESSING')

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get(self):
        return self._client_as(self.owner).get(f'/recordings/{self.recording.recording_id}/')

    def _make_stt_succeeded_with_original(self, text='gaadi tez chal rahi thi', language='hi-IN'):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        return Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text=text, detected_language_code=language,
            provider_name='sarvam', provider_metadata={'model': 'saaras:v3', 'request_id': 'req-1'},
        )

    def test_stt_pending_leaves_translation_status_null(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='PENDING')
        response = self._get()
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'PENDING')
        self.assertIsNone(data['translationStatus'])
        self.assertIsNone(data['transcript']['original'])
        self.assertIsNone(data['transcript']['english'])

    def test_stt_succeeded_translation_pending(self):
        self._make_stt_succeeded_with_original()
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='PENDING')
        response = self._get()
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'SUCCEEDED')
        self.assertEqual(data['translationStatus'], 'PENDING')
        self.assertIsNotNone(data['transcript']['original'])
        self.assertIsNone(data['transcript']['english'])
        self.assertIn('translation in progress', response.data['message'])

    def test_translation_running(self):
        self._make_stt_succeeded_with_original()
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='RUNNING')
        response = self._get()
        data = response.data['data']
        self.assertEqual(data['translationStatus'], 'RUNNING')
        self.assertIsNone(data['transcript']['english'])

    def test_translation_failed_returns_error_code_not_raw_message(self):
        self._make_stt_succeeded_with_original()
        ProcessingJob.objects.create(
            recording=self.recording, job_type='TRANSLATION', status='FAILED',
            error_code='TRANSLATION_UNSUPPORTED_INPUT',
            error_message='Sarvam translation error: {"secret": "internal detail"}',
        )
        response = self._get()
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'SUCCEEDED')
        self.assertEqual(data['translationStatus'], 'FAILED')
        self.assertEqual(data['translationFailureReason'], 'TRANSLATION_UNSUPPORTED_INPUT')
        self.assertIsNone(data['transcript']['english'])
        # The original transcript must remain available even though translation failed.
        self.assertIsNotNone(data['transcript']['original'])
        self.assertNotIn('secret', str(response.data))

    def test_translation_succeeded_returns_both_transcripts(self):
        self._make_stt_succeeded_with_original(text='gaadi tez chal rahi thi', language='hi-IN')
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ENGLISH', text='the vehicle was speeding',
            detected_language_code='en-IN', provider_name='sarvam',
            provider_metadata={'model': 'sarvam-translate:v1', 'chunk_count': 1, 'source_language_code': 'hi-IN'},
        )
        response = self._get()
        data = response.data['data']
        self.assertEqual(data['processingStatus'], 'SUCCEEDED')
        self.assertEqual(data['translationStatus'], 'SUCCEEDED')
        self.assertEqual(data['transcript']['original']['text'], 'gaadi tez chal rahi thi')
        self.assertEqual(data['transcript']['original']['language'], 'hi-IN')
        self.assertEqual(data['transcript']['english']['text'], 'the vehicle was speeding')
        self.assertEqual(data['transcript']['english']['language'], 'en-IN')
        self.assertIn('successfully', response.data['message'])

    def test_raw_sarvam_response_not_exposed_for_translation(self):
        self._make_stt_succeeded_with_original()
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ENGLISH', text='the vehicle was speeding',
            detected_language_code='en-IN', provider_name='sarvam',
            provider_metadata={'model': 'sarvam-translate:v1', 'chunk_count': 3, 'request_id': 'req-should-not-leak'},
        )
        response = self._get()
        self.assertNotIn('request_id', str(response.data))
        self.assertNotIn('chunk_count', str(response.data))
        self.assertNotIn('providerMetadata', response.data['data']['transcript']['english'])

    def test_response_follows_pms_envelope_with_translation_fields(self):
        self._make_stt_succeeded_with_original()
        response = self._get()
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})
        expected_data_keys = {
            'recordingId', 'processingStatus', 'translationStatus', 'extractionStatus', 'transcript', 'edar',
            'failureReason', 'translationFailureReason', 'extractionFailureReason', 'extractionIssues',
        }
        self.assertEqual(set(response.data['data'].keys()), expected_data_keys)

    def test_get_never_creates_or_mutates_a_translation_job(self):
        self._make_stt_succeeded_with_original()
        # No TRANSLATION ProcessingJob exists yet (simulating a recording whose STT
        # succeeded through some path other than csc_apps.processing.stt_service,
        # e.g. a fixture in another test) - GET must not create one, start one, or
        # call Sarvam; it only reads.
        self._get()
        self.assertFalse(ProcessingJob.objects.filter(recording=self.recording, job_type='TRANSLATION').exists())


class RecordingDetailExtractionAPITests(TestCase):
    """GET /recordings/<id>/ - the Phase 4 extension
    (docs/phase4-gemini-edar-extraction.md §API behavior, §Testing D). Reuses the
    exact authorization scenarios Phase 2/3 already established for this endpoint -
    covered again here specifically against the eDAR data path, not just transcript
    fields, per source instructions §66."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner4@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='other4@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer4@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin4@example.com', password='pw', name='Admin One', role='ADMIN'
        )
        self.recording = Recording.objects.create(officer=self.owner, status='PROCESSING')
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='gaadi tez chal rahi thi',
            detected_language_code='hi-IN', provider_name='sarvam',
        )
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='SUCCEEDED')
        Transcript.objects.create(
            recording=self.recording, language='ENGLISH', text='The vehicle was speeding',
            detected_language_code='en-IN', provider_name='sarvam',
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get(self, user=None):
        client = self._client_as(user) if user else APIClient()
        return client.get(f'/recordings/{self.recording.recording_id}/')

    def _make_extraction_succeeded_with_fields(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='SUCCEEDED')
        edar_record = EdarRecord.objects.create(recording=self.recording)
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='crash_type', layer='AI', known='KNOWN',
            value='rear-end collision', confidence=0.9, source_transcript_segment='hit the rear',
            extraction_version='gemini-3.8-flash/prompt-v1/schema-0.1.0',
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='weather_at_time_of_crash', layer='AI', known='UNKNOWN',
            extraction_version='gemini-3.8-flash/prompt-v1/schema-0.1.0',
        )
        return edar_record

    def test_unauthenticated_request_is_rejected(self):
        self.assertEqual(self._get().status_code, 401)

    def test_owner_can_access_own_recording(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        self.assertEqual(response.status_code, 200, response.data)

    def test_reviewer_can_access_any_recording(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.reviewer)
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_can_access_any_recording(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_other_officer_cannot_access_someone_elses_edar_data(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.other_officer)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])

    def test_extraction_pending_returns_null_edar(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['extractionStatus'], 'PENDING')
        self.assertIsNone(data['edar'])
        # Transcripts remain available even while extraction is pending.
        self.assertIsNotNone(data['transcript']['original'])
        self.assertIsNotNone(data['transcript']['english'])

    def test_extraction_running_returns_null_edar(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='RUNNING')
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['extractionStatus'], 'RUNNING')
        self.assertIsNone(data['edar'])

    def test_extraction_failed_returns_error_code_not_raw_message(self):
        ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', status='FAILED',
            error_code='EXTRACTION_SCHEMA_VALIDATION_FAILED',
            error_message='Gemini API error: {"secret": "internal detail"}',
        )
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['extractionStatus'], 'FAILED')
        self.assertEqual(data['extractionFailureReason'], 'EXTRACTION_SCHEMA_VALIDATION_FAILED')
        self.assertIsNone(data['edar'])
        self.assertNotIn('secret', str(response.data))
        # Transcripts remain available even though extraction failed.
        self.assertIsNotNone(data['transcript']['original'])
        self.assertIsNotNone(data['transcript']['english'])

    def test_extraction_succeeded_returns_ai_layer_field_data(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['extractionStatus'], 'SUCCEEDED')
        self.assertEqual(data['edar']['layer'], 'AI')
        self.assertEqual(data['edar']['fields']['crash_type']['value'], 'rear-end collision')
        self.assertEqual(data['edar']['fields']['crash_type']['known'], 'KNOWN')
        self.assertEqual(data['edar']['fields']['crash_type']['confidence'], 0.9)

    def test_unknown_fields_are_represented_not_omitted(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        weather_field = response.data['data']['edar']['fields']['weather_at_time_of_crash']
        self.assertEqual(weather_field['known'], 'UNKNOWN')
        self.assertIsNone(weather_field['value'])

    def test_existing_transcript_response_is_unaffected_by_extraction_fields(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        data = response.data['data']
        self.assertEqual(data['transcript']['original']['text'], 'gaadi tez chal rahi thi')
        self.assertEqual(data['transcript']['english']['text'], 'The vehicle was speeding')

    def test_field_evidence_is_exposed_but_raw_provider_internals_are_not(self):
        # Phase 5 intentionally exposes per-field evidence (a reference into the English
        # transcript) behind the same Recording authorization; Phase 4 deferred this.
        # Raw Gemini payloads, request IDs and API details must still never appear.
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        fields = response.data['data']['edar']['fields']
        self.assertEqual(fields['crash_type']['evidence'], 'hit the rear')
        rendered = str(response.data)
        for forbidden in ('request_id', 'api_key', 'GEMINI', 'Traceback', 'response_json_schema'):
            self.assertNotIn(forbidden, rendered)

    def test_response_follows_pms_envelope(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})
        self.assertTrue(response.data['status'])

    def test_get_never_creates_or_mutates_an_extraction_job(self):
        # No EXTRACTION ProcessingJob exists yet - GET must not create one, start
        # one, or call Gemini; it only reads (docs/phase4-gemini-edar-extraction.md
        # §33 in the source instructions - mandatory).
        self._get(self.owner)
        self.assertFalse(ProcessingJob.objects.filter(recording=self.recording, job_type='EXTRACTION').exists())
        self.assertFalse(EdarRecord.objects.filter(recording=self.recording).exists())


class RecordingEdarApprovalAPITests(TestCase):
    """POST /recordings/<id>/edar/approve/ - Phase 6 officer review + approval
    (docs/phase6-officer-review-approval.md §Testing). Reuses the exact
    authorization scenarios Phase 2-5 established for this same recording
    resource - approval is a write on the same resource GET already authorizes."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner6@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='other6@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer6@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin6@example.com', password='pw', name='Admin One', role='ADMIN'
        )
        self.recording = Recording.objects.create(officer=self.owner, status='READY_FOR_REVIEW')

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get(self, user):
        return self._client_as(user).get(f'/recordings/{self.recording.recording_id}/')

    def _approve(self, user=None, fields=None):
        client = self._client_as(user) if user else APIClient()
        return client.post(
            f'/recordings/{self.recording.recording_id}/edar/approve/', {'fields': fields or {}}, format='json'
        )

    def _make_ai_candidate(self):
        ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='SUCCEEDED')
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 48', confidence=0.91, source_transcript_segment='The crash occurred on NH 48.',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='weather_at_time_of_crash', layer='AI', known='UNKNOWN',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        return edar_record

    def test_unauthenticated_request_is_rejected(self):
        self._make_ai_candidate()
        self.assertEqual(self._approve().status_code, 401)

    def test_owner_can_approve(self):
        self._make_ai_candidate()
        response = self._approve(self.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['status'])

    def test_reviewer_can_approve(self):
        self._make_ai_candidate()
        response = self._approve(self.reviewer)
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_can_approve(self):
        self._make_ai_candidate()
        response = self._approve(self.admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_other_officer_cannot_approve(self):
        self._make_ai_candidate()
        response = self._approve(self.other_officer)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])
        self.assertFalse(EdarFieldValue.objects.filter(layer='APPROVED').exists())

    def test_no_ai_candidate_rejected(self):
        response = self._approve(self.owner)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EdarFieldValue.objects.filter(layer='APPROVED').exists())

    def test_already_approved_rejected_no_duplicate(self):
        self._make_ai_candidate()
        first = self._approve(self.owner)
        self.assertEqual(first.status_code, 200)
        second = self._approve(self.owner, fields={'road_name': {'known': 'KNOWN', 'value': 'Somewhere else'}})
        self.assertEqual(second.status_code, 400)
        approved = EdarFieldValue.objects.filter(layer='APPROVED', field_key='road_name')
        self.assertEqual(approved.count(), 1)
        self.assertEqual(approved.first().value, 'NH 48')

    def test_invalid_value_rejected_no_approved_rows_created(self):
        self._make_ai_candidate()
        response = self._approve(self.owner, fields={'road_name': {'known': 'KNOWN', 'value': 123}})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EdarFieldValue.objects.filter(layer='APPROVED').exists())

    def test_get_before_approval_shows_pending_review_and_null_approved(self):
        self._make_ai_candidate()
        response = self._get(self.owner)
        edar = response.data['data']['edar']
        self.assertEqual(edar['reviewStatus'], 'PENDING_REVIEW')
        self.assertIsNone(edar['approved'])
        self.assertEqual(edar['fields']['road_name']['value'], 'NH 48')

    def test_get_after_approval_shows_approved_alongside_unchanged_ai(self):
        self._make_ai_candidate()
        self._approve(self.owner, fields={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}})
        response = self._get(self.owner)
        edar = response.data['data']['edar']
        self.assertEqual(edar['reviewStatus'], 'APPROVED')
        self.assertEqual(edar['layer'], 'AI')
        # AI view is untouched.
        self.assertEqual(edar['fields']['road_name']['value'], 'NH 48')
        self.assertEqual(edar['fields']['road_name']['confidence'], 0.91)
        # APPROVED view reflects the officer's edit.
        self.assertEqual(edar['approved']['fields']['road_name']['value'], 'NH 48, Ahmedabad')
        self.assertEqual(edar['approved']['reviewedBy']['userId'], self.owner.user_id)
        self.assertIsNotNone(edar['approved']['reviewedAt'])

    def test_multiple_field_edits_via_http(self):
        self._make_ai_candidate()
        response = self._approve(self.owner, fields={
            'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'},
            'weather_at_time_of_crash': {'known': 'KNOWN', 'value': 'clear'},
        })
        self.assertEqual(response.status_code, 200, response.data)
        approved_fields = response.data['data']['edar']['approved']['fields']
        self.assertEqual(approved_fields['road_name']['value'], 'NH 48, Ahmedabad')
        self.assertEqual(approved_fields['weather_at_time_of_crash'], {'value': 'clear', 'known': 'KNOWN'})

    def test_response_follows_pms_envelope(self):
        self._make_ai_candidate()
        response = self._approve(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})

    def test_no_provider_internals_or_secrets_in_response(self):
        self._make_ai_candidate()
        response = self._approve(self.owner, fields={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}})
        rendered = str(response.data)
        for forbidden in ('request_id', 'api_key', 'GEMINI', 'Traceback', 'SARVAM'):
            self.assertNotIn(forbidden, rendered)

    def test_approval_does_not_mutate_ai_rows(self):
        edar_record = self._make_ai_candidate()
        before = {
            r.field_value_id: (r.known, r.value, r.confidence, r.source_transcript_segment, r.extraction_version)
            for r in EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI')
        }
        self._approve(self.owner, fields={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}})
        after = {
            r.field_value_id: (r.known, r.value, r.confidence, r.source_transcript_segment, r.extraction_version)
            for r in EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI')
        }
        self.assertEqual(before, after)

    def test_recording_advances_to_completed_after_approval(self):
        self._make_ai_candidate()
        self._approve(self.owner)
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'COMPLETED')

    def test_client_cannot_set_approving_user_or_layer_via_body(self):
        # The request serializer has no reviewedBy/approvedBy/layer field at all -
        # any such key in the body is simply ignored, never trusted.
        self._make_ai_candidate()
        client = self._client_as(self.owner)
        response = client.post(
            f'/recordings/{self.recording.recording_id}/edar/approve/',
            {'fields': {}, 'reviewedBy': 999, 'layer': 'APPROVED', 'approvedBy': 999},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['edar']['approved']['reviewedBy']['userId'], self.owner.user_id)


class RecordingListAPITests(TestCase):
    """GET /recordings/ - Phase 7 history/search (docs/phase7-history-search.md).
    Shares its URL with POST /recordings/ (Phase 1 upload); dispatch itself is
    covered by test_get_and_post_share_one_path below."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner7@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='other7@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer7@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin7@example.com', password='pw', name='Admin One', role='ADMIN'
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _list(self, user=None, **query):
        client = self._client_as(user) if user else APIClient()
        return client.get('/recordings/', query)

    def _make_recording(
        self, officer, status='PROCESSING', road_name=None, case_fir_number=None,
        stt='SUCCEEDED', translation=None, extraction=None, review_status=None,
    ):
        recording = Recording.objects.create(
            officer=officer, status=status, road_name=road_name, case_fir_number=case_fir_number,
        )
        if stt:
            ProcessingJob.objects.create(recording=recording, job_type='STT', status=stt)
        if translation:
            ProcessingJob.objects.create(recording=recording, job_type='TRANSLATION', status=translation)
        if extraction:
            ProcessingJob.objects.create(recording=recording, job_type='EXTRACTION', status=extraction)
        if review_status:
            EdarRecord.objects.create(recording=recording, review_status=review_status)
        return recording

    # --- Basic history ---------------------------------------------------

    def test_unauthenticated_request_is_rejected(self):
        self.assertEqual(self._list().status_code, 401)

    def test_authenticated_history_request_returns_own_recordings(self):
        self._make_recording(self.owner)
        response = self._list(self.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['data']['data']), 1)

    def test_empty_history_returns_200_with_empty_list_not_404(self):
        response = self._list(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['data'], [])
        self.assertTrue(response.data['status'])

    def test_deterministic_ordering_newest_first(self):
        first = self._make_recording(self.owner)
        second = self._make_recording(self.owner)
        response = self._list(self.owner)
        ids = [row['recordingId'] for row in response.data['data']['data']]
        self.assertEqual(ids, [second.recording_id, first.recording_id])

    def test_pagination_first_and_subsequent_page(self):
        for _ in range(3):
            self._make_recording(self.owner)
        first_page = self._list(self.owner, limit=2, page_num=1)
        self.assertEqual(len(first_page.data['data']['data']), 2)
        self.assertEqual(first_page.data['data']['presentPage'], 1)
        self.assertEqual(first_page.data['data']['totalPage'], 2)
        self.assertIn('nextPageUrl', first_page.data['data'])

        second_page = self._list(self.owner, limit=2, page_num=2)
        self.assertEqual(len(second_page.data['data']['data']), 1)
        self.assertEqual(second_page.data['data']['presentPage'], 2)
        first_ids = {row['recordingId'] for row in first_page.data['data']['data']}
        second_ids = {row['recordingId'] for row in second_page.data['data']['data']}
        self.assertEqual(first_ids & second_ids, set())

    def test_page_size_respects_limit(self):
        for _ in range(5):
            self._make_recording(self.owner)
        response = self._list(self.owner, limit=3)
        self.assertEqual(len(response.data['data']['data']), 3)

    # --- Authorization / IDOR --------------------------------------------

    def test_owner_sees_own_recording(self):
        recording = self._make_recording(self.owner)
        response = self._list(self.owner)
        self.assertIn(recording.recording_id, [r['recordingId'] for r in response.data['data']['data']])

    def test_other_officer_cannot_see_owners_recording(self):
        self._make_recording(self.owner)
        response = self._list(self.other_officer)
        self.assertEqual(response.data['data']['data'], [])

    def test_reviewer_sees_only_their_own_recordings_not_everyone_elses(self):
        # Deliberate Phase 7 scope decision (docs/phase7-history-search.md
        # §Authorization): narrower than GET/<id>/'s owner-OR-REVIEWER/ADMIN rule.
        self._make_recording(self.owner)
        own = self._make_recording(self.reviewer)
        response = self._list(self.reviewer)
        ids = [r['recordingId'] for r in response.data['data']['data']]
        self.assertEqual(ids, [own.recording_id])

    def test_admin_sees_only_their_own_recordings(self):
        self._make_recording(self.owner)
        own = self._make_recording(self.admin)
        response = self._list(self.admin)
        ids = [r['recordingId'] for r in response.data['data']['data']]
        self.assertEqual(ids, [own.recording_id])

    def test_reviewer_can_still_open_hidden_recording_directly_by_id(self):
        # Unchanged Phase 2-6 behavior on the existing detail endpoint - only the
        # LIST is scoped narrower in Phase 7, not GET/<id>/ itself.
        recording = self._make_recording(self.owner)
        client = self._client_as(self.reviewer)
        response = client.get(f'/recordings/{recording.recording_id}/')
        self.assertEqual(response.status_code, 200)

    def test_filter_cannot_reveal_another_officers_recording(self):
        self._make_recording(self.owner, road_name='NH 48', case_fir_number='FIR-0099')
        response = self._list(self.other_officer, road_name='NH', case_fir_number='FIR-0099')
        self.assertEqual(response.data['data']['data'], [])

    def test_idor_search_by_exact_case_fir_number_does_not_leak_across_officers(self):
        self._make_recording(self.owner, case_fir_number='FIR-SECRET-1')
        response = self._list(self.other_officer, case_fir_number='FIR-SECRET-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['data'], [])

    def test_authorization_filter_applied_at_query_level_not_python_post_filter(self):
        # If authorization were applied after fetching (Python-side), a page_num/
        # limit combination could still be used to enumerate rows before they are
        # discarded. Assert the other officer's own empty result set has
        # totalPage/presentPage consistent with zero *accessible* rows, not with
        # the other officer's one existing (but inaccessible) recording.
        self._make_recording(self.owner)
        response = self._list(self.other_officer)
        self.assertEqual(response.data['data']['totalPage'], 1)
        self.assertEqual(response.data['data']['data'], [])

    # --- Search / filtering ------------------------------------------------

    def test_filter_by_status(self):
        self._make_recording(self.owner, status='COMPLETED')
        self._make_recording(self.owner, status='PROCESSING')
        response = self._list(self.owner, status='COMPLETED')
        rows = response.data['data']['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'COMPLETED')

    def test_filter_by_review_status(self):
        self._make_recording(self.owner, review_status='APPROVED')
        self._make_recording(self.owner, review_status='PENDING_REVIEW')
        self._make_recording(self.owner)  # no EdarRecord at all
        response = self._list(self.owner, review_status='APPROVED')
        rows = response.data['data']['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reviewStatus'], 'APPROVED')

    def test_filter_by_case_fir_number_is_exact_match(self):
        self._make_recording(self.owner, case_fir_number='FIR-2026-001')
        self._make_recording(self.owner, case_fir_number='FIR-2026-0011')
        response = self._list(self.owner, case_fir_number='FIR-2026-001')
        rows = response.data['data']['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['caseFirNumber'], 'FIR-2026-001')

    def test_filter_by_road_name_is_partial_case_insensitive_match(self):
        self._make_recording(self.owner, road_name='National Highway 48, near Vadodara')
        self._make_recording(self.owner, road_name='SH 12')
        response = self._list(self.owner, road_name='highway 48')
        rows = response.data['data']['data']
        self.assertEqual(len(rows), 1)
        self.assertIn('National Highway 48', rows[0]['roadName'])

    def test_filter_by_created_date_range(self):
        old = self._make_recording(self.owner)
        Recording.objects.filter(pk=old.pk).update(created_at='2020-01-01T00:00:00Z')
        recent = self._make_recording(self.owner)
        response = self._list(self.owner, created_from='2025-01-01')
        ids = [r['recordingId'] for r in response.data['data']['data']]
        self.assertEqual(ids, [recent.recording_id])

    def test_multiple_filters_combine_with_and_not_or(self):
        self._make_recording(self.owner, status='COMPLETED', road_name='NH 48')
        self._make_recording(self.owner, status='PROCESSING', road_name='NH 48')
        self._make_recording(self.owner, status='COMPLETED', road_name='SH 12')
        response = self._list(self.owner, status='COMPLETED', road_name='NH 48')
        self.assertEqual(len(response.data['data']['data']), 1)

    def test_no_match_search_returns_empty_not_error(self):
        self._make_recording(self.owner, road_name='NH 48')
        response = self._list(self.owner, road_name='does-not-exist-anywhere')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['data'], [])

    def test_invalid_status_filter_is_rejected(self):
        response = self._list(self.owner, status='NOT_A_REAL_STATUS')
        self.assertEqual(response.status_code, 400)

    def test_invalid_date_filter_is_rejected(self):
        response = self._list(self.owner, created_from='not-a-date')
        self.assertEqual(response.status_code, 400)

    def test_invalid_page_num_is_rejected(self):
        self._make_recording(self.owner)
        response = self._list(self.owner, page_num=999)
        self.assertEqual(response.status_code, 400)

    def test_limit_above_maximum_is_rejected(self):
        response = self._list(self.owner, limit=10000)
        self.assertEqual(response.status_code, 400)

    # --- Layer semantics -----------------------------------------------

    def test_approved_record_shows_approved_review_status(self):
        self._make_recording(self.owner, review_status='APPROVED')
        response = self._list(self.owner)
        self.assertEqual(response.data['data']['data'][0]['reviewStatus'], 'APPROVED')

    def test_pending_record_shows_pending_review_status_not_approved(self):
        self._make_recording(self.owner, review_status='PENDING_REVIEW')
        response = self._list(self.owner)
        self.assertEqual(response.data['data']['data'][0]['reviewStatus'], 'PENDING_REVIEW')

    def test_no_edar_record_yet_shows_null_review_status_not_fabricated(self):
        self._make_recording(self.owner)
        response = self._list(self.owner)
        self.assertIsNone(response.data['data']['data'][0]['reviewStatus'])

    def test_list_never_exposes_raw_edar_field_values(self):
        recording = self._make_recording(self.owner, review_status='APPROVED')
        edar_record = EdarRecord.objects.get(recording=recording)
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='crash_type', layer='AI', known='KNOWN',
            value='SECRET-AI-VALUE', confidence=0.9, extraction_version='v',
        )
        response = self._list(self.owner)
        self.assertNotIn('SECRET-AI-VALUE', str(response.data))
        self.assertNotIn('fields', response.data['data']['data'][0])

    # --- API shape --------------------------------------------------------

    def test_response_follows_pms_envelope(self):
        response = self._list(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})

    def test_pagination_metadata_shape(self):
        self._make_recording(self.owner)
        response = self._list(self.owner)
        self.assertEqual(set(response.data['data'].keys()) - {'nextPageUrl', 'previousPageUrl'}, {'data', 'presentPage', 'totalPage'})

    def test_list_item_shape_is_concise(self):
        self._make_recording(self.owner, road_name='NH 48', case_fir_number='FIR-1', review_status='APPROVED')
        row = self._list(self.owner).data['data']['data'][0]
        self.assertEqual(set(row.keys()), {
            'recordingId', 'status', 'createdAt', 'roadName', 'caseFirNumber',
            'policeStationJurisdiction', 'processingStatus', 'translationStatus',
            'extractionStatus', 'reviewStatus',
        })

    def test_get_and_post_share_one_path(self):
        response = self._client_as(self.owner).put('/recordings/')
        self.assertEqual(response.status_code, 405)

    # --- Performance / query behavior --------------------------------------

    def test_list_query_count_does_not_scale_with_recording_count(self):
        for _ in range(2):
            self._make_recording(self.owner, translation='SUCCEEDED', extraction='SUCCEEDED', review_status='APPROVED')
        with CaptureQueriesContext(connection) as small:
            self._list(self.owner, limit=10)
        for _ in range(8):
            self._make_recording(self.owner, translation='SUCCEEDED', extraction='SUCCEEDED', review_status='APPROVED')
        with CaptureQueriesContext(connection) as large:
            self._list(self.owner, limit=10)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))

    def test_list_does_not_issue_one_query_per_recording(self):
        for _ in range(6):
            self._make_recording(self.owner, translation='SUCCEEDED', extraction='SUCCEEDED', review_status='APPROVED')
        with CaptureQueriesContext(connection) as ctx:
            response = self._list(self.owner, limit=10)
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(ctx.captured_queries), 10)

    # --- Security -----------------------------------------------------

    def test_no_secrets_or_internals_in_list_response(self):
        self._make_recording(self.owner)
        response = self._list(self.owner)
        rendered = str(response.data)
        for forbidden in ('request_id', 'api_key', 'GEMINI', 'SARVAM', 'Traceback', 'Bearer'):
            self.assertNotIn(forbidden, rendered)


class RecordingExportAPITests(TestCase):
    """GET /recordings/<id>/export/ - Phase 8 approved-eDAR export
    (docs/phase8-export.md). Reuses the exact authorization scenarios Phase 2-7
    already established for this resource."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='owner8@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='other8@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='reviewer8@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='admin8@example.com', password='pw', name='Admin One', role='ADMIN'
        )
        self.recording = Recording.objects.create(
            officer=self.owner, status='READY_FOR_REVIEW', case_fir_number='FIR-2026-042',
            gps_latitude=22.3072, gps_longitude=73.1812,
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _export(self, user=None):
        client = self._client_as(user) if user else APIClient()
        return client.get(f'/recordings/{self.recording.recording_id}/export/')

    def _ai_row(self, edar_record, field_key, known='KNOWN', value=None, evidence='evidence'):
        return EdarFieldValue.objects.create(
            edar_record=edar_record, field_key=field_key, layer='AI', known=known, value=value,
            confidence=0.9 if known == 'KNOWN' else None,
            source_transcript_segment=evidence if known == 'KNOWN' else None,
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )

    def _make_and_approve(self, edits=None):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self._ai_row(edar_record, 'road_name', value='NH 48')
        self._ai_row(edar_record, 'crash_date', known='UNKNOWN')
        self._ai_row(edar_record, 'hit_and_run_flag', value=False)
        self._ai_row(edar_record, 'vehicle.1.vehicle_type', value='motorcycle')
        self._ai_row(edar_record, 'vehicle.2.vehicle_type', value='car')
        self._ai_row(edar_record, 'casualty.1.person_type', value='rider')
        self._ai_row(edar_record, 'casualty.1.injury_severity', value='grievous injury')
        self._ai_row(edar_record, 'officer_remarks', value='No markings near the curve.')
        approval_service.approve_edar(edar_record=edar_record, officer=self.owner, edits=edits or {})
        return edar_record

    # --- Authorization / IDOR ------------------------------------------

    def test_unauthenticated_request_is_rejected(self):
        self._make_and_approve()
        self.assertEqual(self._export().status_code, 401)

    def test_owner_can_export_approved_recording(self):
        self._make_and_approve()
        response = self._export(self.owner)
        self.assertEqual(response.status_code, 200, response.data)

    def test_reviewer_can_export(self):
        self._make_and_approve()
        response = self._export(self.reviewer)
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_can_export(self):
        self._make_and_approve()
        response = self._export(self.admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_other_officer_cannot_export(self):
        self._make_and_approve()
        response = self._export(self.other_officer)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])

    def test_missing_recording_returns_existing_not_found_behavior(self):
        client = self._client_as(self.owner)
        response = client.get('/recordings/999999/export/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('not found', response.data['message'].lower())

    # --- Approval requirement -------------------------------------------

    def test_no_eDAR_extraction_at_all_is_rejected(self):
        response = self._export(self.owner)
        self.assertEqual(response.status_code, 400)

    def test_ai_only_record_not_approved_is_rejected(self):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self._ai_row(edar_record, 'road_name', value='NH 48')
        response = self._export(self.owner)
        self.assertEqual(response.status_code, 400)
        self.assertIn('not been approved', response.data['message'])

    def test_structurally_incomplete_approved_snapshot_is_rejected_not_fabricated(self):
        # review_status says APPROVED but (defensively, a state Phase 6's own
        # invariant should prevent) zero APPROVED rows exist - export must fail
        # loudly, never fabricate a dataset.
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='APPROVED')
        response = self._export(self.owner)
        self.assertEqual(response.status_code, 400)

    def test_already_exported_recording_can_be_exported_again(self):
        # Export is read-only - repeatability is expected, not a "duplicate" error.
        self._make_and_approve()
        first = self._export(self.owner)
        second = self._export(self.owner)
        self.assertEqual((first.status_code, second.status_code), (200, 200))

    # --- AI vs APPROVED (mandatory) --------------------------------------

    def test_approved_edit_appears_in_export_ai_value_never_does(self):
        self._make_and_approve(edits={'road_name': {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'}})
        response = self._export(self.owner)
        road_name = response.data['data']['eDAR']['crashIdentification']['road_name']
        self.assertEqual(road_name, {'known': 'KNOWN', 'value': 'NH 48, Ahmedabad'})
        self.assertNotIn('NH 48', str(response.data).replace('NH 48, Ahmedabad', ''))

    def test_ai_row_mutated_after_approval_does_not_leak_into_export(self):
        edar_record = self._make_and_approve()
        ai_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='road_name', layer='AI')
        ai_row.value = 'TAMPERED-AI-VALUE-SHOULD-NEVER-APPEAR'
        ai_row.save(update_fields=['value'])

        response = self._export(self.owner)
        self.assertNotIn('TAMPERED-AI-VALUE-SHOULD-NEVER-APPEAR', str(response.data))
        self.assertEqual(response.data['data']['eDAR']['crashIdentification']['road_name']['value'], 'NH 48')

    def test_export_never_reads_ai_layer_for_any_field(self):
        edar_record = self._make_and_approve()
        EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI').update(
            value='TAMPERED', confidence=0.01,
        )
        response = self._export(self.owner)
        self.assertNotIn('TAMPERED', str(response.data))

    # --- Content -----------------------------------------------------------

    def test_export_contains_approved_values_correctly_grouped(self):
        self._make_and_approve()
        edar = self._export(self.owner).data['data']['eDAR']
        self.assertEqual(edar['crashIdentification']['road_name'], {'known': 'KNOWN', 'value': 'NH 48'})
        self.assertEqual(edar['crashCircumstances']['hit_and_run_flag'], {'known': 'KNOWN', 'value': False})
        self.assertEqual(edar['officerAssessment']['officer_remarks']['value'], 'No markings near the curve.')

    def test_repeated_vehicle_and_casualty_structures_preserved_not_flattened(self):
        self._make_and_approve()
        edar = self._export(self.owner).data['data']['eDAR']
        self.assertEqual(len(edar['vehicles']), 2)
        self.assertEqual(edar['vehicles'][0]['vehicle_type'], {'known': 'KNOWN', 'value': 'motorcycle'})
        self.assertEqual(edar['vehicles'][1]['vehicle_type'], {'known': 'KNOWN', 'value': 'car'})
        self.assertEqual(len(edar['casualties']), 1)
        self.assertEqual(edar['casualties'][0]['person_type']['value'], 'rider')
        self.assertEqual(edar['casualties'][0]['injury_severity']['value'], 'grievous injury')
        # No dotted vehicle.N./casualty.N. keys anywhere - properly nested, not a
        # flat re-export of the storage representation.
        self.assertNotIn('vehicle.1.vehicle_type', str(edar))

    def test_no_duplicate_or_omitted_approved_fields(self):
        # A realistic, COMPLETE AI candidate (every flat field, matching what
        # csc_apps.processing.extraction_service._build_rows always produces for a
        # real Gemini run - docs/phase4-gemini-edar-extraction.md), not the minimal
        # subset _make_and_approve() uses elsewhere in this class. Proves the
        # export's field-for-field completeness against the real production
        # guarantee, not an artificial fixture.
        from csc_apps.edar.schema_loader import load_schema
        from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys, repeating_field_keys

        schema = load_schema()
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        for key in flat_field_keys(schema):
            self._ai_row(edar_record, key, known='UNKNOWN')
        for base_key in repeating_field_keys(schema, 'vehicle'):
            self._ai_row(edar_record, f'vehicle.1.{base_key}', value='x')
        for base_key in repeating_field_keys(schema, 'casualty'):
            self._ai_row(edar_record, f'casualty.1.{base_key}', value='x')
        approval_service.approve_edar(edar_record=edar_record, officer=self.owner, edits={})

        approved_keys = set(
            EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED').values_list('field_key', flat=True)
        )
        edar = self._export(self.owner).data['data']['eDAR']
        exported_keys = set(edar['crashIdentification']) | set(edar['roadEnvironment']) | set(edar['crashCircumstances'])
        exported_keys |= set(edar['infrastructureObservations']) | set(edar['officerAssessment'])
        exported_keys |= {f'vehicle.{i + 1}.{k}' for i, v in enumerate(edar['vehicles']) for k in v}
        exported_keys |= {f'casualty.{i + 1}.{k}' for i, v in enumerate(edar['casualties']) for k in v}
        # gps_coordinates is the one Module A key never stored as an EdarFieldValue
        # row at all (docs/phase8-export.md) - excluded from both sides on purpose.
        self.assertEqual(approved_keys, exported_keys)

    def test_unknown_known_state_preserved_not_converted_to_empty(self):
        self._make_and_approve()
        crash_date = self._export(self.owner).data['data']['eDAR']['crashIdentification']['crash_date']
        self.assertEqual(crash_date, {'known': 'UNKNOWN', 'value': None})

    def test_not_applicable_and_uncertain_known_states_preserved(self):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self._ai_row(edar_record, 'road_name', value='NH 48')
        approval_service.approve_edar(
            edar_record=edar_record, officer=self.owner,
            edits={
                'road_name': {'known': 'NOT_APPLICABLE', 'value': None},
            },
        )
        edar = self._export(self.owner).data['data']['eDAR']
        self.assertEqual(edar['crashIdentification']['road_name'], {'known': 'NOT_APPLICABLE', 'value': None})

    def test_gps_coordinates_come_from_recording_not_eDAR_layer(self):
        self._make_and_approve()
        data = self._export(self.owner).data['data']
        self.assertEqual(data['gpsCoordinates']['latitude'], 22.3072)
        self.assertEqual(data['gpsCoordinates']['longitude'], 73.1812)
        self.assertNotIn('gps_coordinates', data['eDAR']['crashIdentification'])

    def test_gps_coordinates_null_when_not_captured(self):
        self.recording.gps_latitude = None
        self.recording.gps_longitude = None
        self.recording.save(update_fields=['gps_latitude', 'gps_longitude'])
        self._make_and_approve()
        self.assertIsNone(self._export(self.owner).data['data']['gpsCoordinates'])

    def test_export_metadata_present(self):
        self._make_and_approve()
        data = self._export(self.owner).data['data']
        self.assertEqual(data['recordingId'], self.recording.recording_id)
        self.assertEqual(data['caseFirNumber'], 'FIR-2026-042')
        self.assertEqual(data['reviewStatus'], 'APPROVED')
        self.assertEqual(data['approvedBy']['userId'], self.owner.user_id)
        self.assertIsNotNone(data['approvedAt'])

    # --- API shape / security ------------------------------------------

    def test_response_follows_pms_envelope(self):
        self._make_and_approve()
        response = self._export(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})

    def test_response_content_type_is_json(self):
        self._make_and_approve()
        response = self._export(self.owner)
        self.assertIn('application/json', response['Content-Type'])

    def test_no_content_disposition_header_not_a_file_download(self):
        # docs/phase8-export.md §Response shape - a normal enveloped API response,
        # not a raw downloadable attachment (no such precedent exists in PMS).
        self._make_and_approve()
        response = self._export(self.owner)
        self.assertNotIn('Content-Disposition', response)

    def test_no_secrets_transcripts_or_provider_internals_in_export(self):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self._ai_row(edar_record, 'road_name', value='NH 48', evidence='SECRET-TRANSCRIPT-TEXT')
        approval_service.approve_edar(edar_record=edar_record, officer=self.owner, edits={})
        response = self._export(self.owner)
        rendered = str(response.data)
        for forbidden in (
            'SECRET-TRANSCRIPT-TEXT', 'request_id', 'api_key', 'GEMINI', 'SARVAM',
            'confidence', 'evidence', 'extraction_version', 'Bearer', 'Traceback',
        ):
            self.assertNotIn(forbidden, rendered)

    # --- Performance --------------------------------------------------

    def test_export_query_count_is_bounded(self):
        self._make_and_approve()
        with CaptureQueriesContext(connection) as ctx:
            response = self._export(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(ctx.captured_queries), 10)


class RecordingGetAllAPITests(TestCase):
    """GET /recordings/get_all/ - Phase 10A (docs/phase10a-get-all-and-smoke-
    test.md). A lightweight index, deliberately distinct from the paginated
    GET /recordings/ (Phase 7)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='getall_owner@example.com', password='pw', name='Owner Officer', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='getall_other@example.com', password='pw', name='Other Officer', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='getall_reviewer@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.admin = User.objects.create_user(
            email='getall_admin@example.com', password='pw', name='Admin One', role='ADMIN'
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _get_all(self, user=None):
        client = self._client_as(user) if user else APIClient()
        return client.get('/recordings/get_all/')

    def test_unauthenticated_request_is_rejected(self):
        self.assertEqual(self._get_all().status_code, 401)

    def test_authenticated_request_succeeds(self):
        Recording.objects.create(officer=self.owner, status='PROCESSING')
        response = self._get_all(self.owner)
        self.assertEqual(response.status_code, 200, response.data)

    def test_authenticated_user_sees_own_recordings(self):
        recording = Recording.objects.create(officer=self.owner, status='PROCESSING')
        ids = [r['recordingId'] for r in self._get_all(self.owner).data['data']]
        self.assertIn(recording.recording_id, ids)

    def test_another_officers_recordings_are_excluded(self):
        Recording.objects.create(officer=self.other_officer, status='COMPLETED')
        self.assertEqual(self._get_all(self.owner).data['data'], [])

    def test_reviewer_sees_only_their_own_recordings(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        own = Recording.objects.create(officer=self.reviewer, status='PROCESSING')
        ids = [r['recordingId'] for r in self._get_all(self.reviewer).data['data']]
        self.assertEqual(ids, [own.recording_id])

    def test_admin_sees_only_their_own_recordings(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        own = Recording.objects.create(officer=self.admin, status='PROCESSING')
        ids = [r['recordingId'] for r in self._get_all(self.admin).data['data']]
        self.assertEqual(ids, [own.recording_id])

    def test_multiple_statuses_all_appear_not_only_completed(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        Recording.objects.create(officer=self.owner, status='PROCESSING')
        Recording.objects.create(officer=self.owner, status='FAILED')
        Recording.objects.create(officer=self.owner, status='READY_FOR_REVIEW')
        statuses = {r['status'] for r in self._get_all(self.owner).data['data']}
        self.assertEqual(statuses, {'COMPLETED', 'PROCESSING', 'FAILED', 'READY_FOR_REVIEW'})

    def test_completed_recording_shows_completed_status(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        self.assertEqual(self._get_all(self.owner).data['data'][0]['status'], 'COMPLETED')

    def test_processing_recording_shows_its_current_status(self):
        Recording.objects.create(officer=self.owner, status='PROCESSING')
        self.assertEqual(self._get_all(self.owner).data['data'][0]['status'], 'PROCESSING')

    def test_failed_recording_shows_its_current_status(self):
        Recording.objects.create(officer=self.owner, status='FAILED')
        self.assertEqual(self._get_all(self.owner).data['data'][0]['status'], 'FAILED')

    def test_response_contains_only_intended_basic_fields(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED', road_name='NH 48', case_fir_number='FIR-1')
        row = self._get_all(self.owner).data['data'][0]
        self.assertEqual(set(row.keys()), {'recordingId', 'status', 'createdAt'})

    def test_transcript_is_not_exposed(self):
        recording = Recording.objects.create(officer=self.owner, status='PROCESSING')
        ProcessingJob.objects.create(recording=recording, job_type='STT', status='SUCCEEDED')
        Transcript.objects.create(
            recording=recording, language='ORIGINAL', text='SECRET-TRANSCRIPT-TEXT',
            detected_language_code='hi-IN', provider_name='sarvam',
        )
        response = self._get_all(self.owner)
        self.assertNotIn('SECRET-TRANSCRIPT-TEXT', str(response.data))
        self.assertNotIn('transcript', str(response.data).lower())

    def test_edar_is_not_exposed(self):
        recording = Recording.objects.create(officer=self.owner, status='READY_FOR_REVIEW')
        edar_record = EdarRecord.objects.create(recording=recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='SECRET-ROAD-NAME', confidence=0.9, extraction_version='v',
        )
        response = self._get_all(self.owner)
        self.assertNotIn('SECRET-ROAD-NAME', str(response.data))
        self.assertNotIn('edar', str(response.data).lower())

    def test_no_credentials_or_internals_exposed(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        rendered = str(self._get_all(self.owner).data)
        for forbidden in ('request_id', 'api_key', 'GEMINI', 'SARVAM', 'Bearer', 'Traceback'):
            self.assertNotIn(forbidden, rendered)

    def test_deterministic_ordering_newest_first(self):
        first = Recording.objects.create(officer=self.owner, status='COMPLETED')
        second = Recording.objects.create(officer=self.owner, status='PROCESSING')
        ids = [r['recordingId'] for r in self._get_all(self.owner).data['data']]
        self.assertEqual(ids, [second.recording_id, first.recording_id])

    def test_empty_result_returns_200_with_empty_list(self):
        response = self._get_all(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data'], [])

    def test_response_follows_pms_envelope(self):
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        response = self._get_all(self.owner)
        self.assertEqual(set(response.data.keys()), {'status', 'message', 'data'})

    def test_data_is_a_plain_list_not_paginated(self):
        # Deliberately different shape from GET /recordings/ (Phase 7), which
        # wraps its list in {data, presentPage, totalPage, ...}.
        Recording.objects.create(officer=self.owner, status='COMPLETED')
        response = self._get_all(self.owner)
        self.assertIsInstance(response.data['data'], list)

    def test_query_count_does_not_scale_with_recording_count(self):
        for _ in range(3):
            Recording.objects.create(officer=self.owner, status='COMPLETED')
        with CaptureQueriesContext(connection) as small:
            self._get_all(self.owner)
        for _ in range(10):
            Recording.objects.create(officer=self.owner, status='COMPLETED')
        with CaptureQueriesContext(connection) as large:
            self._get_all(self.owner)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        self.assertLessEqual(len(large.captured_queries), 3)
