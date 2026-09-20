import datetime as dt
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
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
            'failureReason', 'translationFailureReason', 'extractionFailureReason',
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

    def test_raw_gemini_response_and_source_evidence_not_exposed(self):
        self._make_extraction_succeeded_with_fields()
        response = self._get(self.owner)
        rendered = str(response.data)
        self.assertNotIn('hit the rear', rendered)  # source_transcript_segment - Phase 5's concern, not Phase 4's
        self.assertNotIn('prompt-v1', rendered)  # extraction_version - internal provenance, not API-exposed
        self.assertNotIn('schema-0.1.0', rendered)

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
