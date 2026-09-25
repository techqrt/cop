"""PUT /recordings/<id>/ - targeted supplemental audio for missing eDAR fields only
(docs/phase10b-supplemental-audio.md). Covers the endpoint's own contract: no
client-supplied field list of any kind, server-side missing-field detection,
authorization/state/approval gating, async (job-creation-only) behavior, and that
the response reuses GET's exact shape. csc_apps.processing.test_phase10b_extraction
covers the targeted-extraction/merge mechanics themselves - this file only proves
the HTTP surface wires up to them correctly.
"""

import tempfile

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from csc_apps.authentication.models import User
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing.models import ProcessingJob
from csc_apps.recordings.models.audio import Audio
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.storage import LocalPrivateAudioStorage
from csc_apps.recordings.tests import _TINY_VALID_WAV, _wav_file


class RecordingSupplementAudioAPITests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.owner = User.objects.create_user(
            email='supp-owner@example.com', password='pw', name='Owner', role='OFFICER'
        )
        self.other_officer = User.objects.create_user(
            email='supp-other@example.com', password='pw', name='Other', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='supp-reviewer@example.com', password='pw', name='Reviewer', role='REVIEWER'
        )

    def _client_as(self, user):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def _put(self, user, recording_id, extra_fields=None):
        data = {'audio': _wav_file()}
        if extra_fields:
            data.update(extra_fields)
        return self._client_as(user).put(f'/recordings/{recording_id}/', data, format='multipart')

    def _original_audio(self, recording):
        storage = LocalPrivateAudioStorage()
        stored = storage.save(recording_id=recording.recording_id, extension='.wav', fileobj=_wav_file())
        return Audio.objects.create(
            recording=recording, role='ORIGINAL', source='UPLOAD', storage_path=stored.storage_path,
            content_type='audio/wav', file_size_bytes=stored.size_bytes, checksum_sha256=stored.checksum_sha256,
        )

    def _make_recording_with_edar(self, status='READY_FOR_REVIEW', review_status='PENDING_REVIEW', all_known=False):
        recording = Recording.objects.create(officer=self.owner, status=status)
        audio = self._original_audio(recording)
        ProcessingJob.objects.create(recording=recording, job_type='STT', audio=audio, status='SUCCEEDED')
        edar_record = EdarRecord.objects.create(recording=recording, review_status=review_status)
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 48', confidence=0.9, extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='weather_at_time_of_crash', layer='AI',
            known='KNOWN' if all_known else 'UNKNOWN',
            value='Clear' if all_known else None,
            confidence=0.8 if all_known else None,
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        return recording, edar_record

    # --- Authentication / authorization -----------------------------------

    def test_unauthenticated_request_is_rejected(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(None, recording.recording_id)
        self.assertEqual(response.status_code, 401)

    def test_owner_can_upload_supplemental_audio(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)

    def test_reviewer_can_upload_supplemental_audio(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(self.reviewer, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)

    def test_other_officer_is_denied(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(self.other_officer, recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_unknown_recording_id_is_rejected(self):
        response = self._put(self.owner, 999999)
        self.assertEqual(response.status_code, 400)

    # --- Preconditions -------------------------------------------------

    def test_rejected_when_no_edar_candidate_exists(self):
        recording = Recording.objects.create(officer=self.owner, status='PROCESSING')
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording).count(), 0)

    def test_rejected_when_recording_not_yet_ready_for_review(self):
        recording = Recording.objects.create(officer=self.owner, status='PROCESSING')
        edar_record = EdarRecord.objects.create(recording=recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='road_name', layer='AI', known='UNKNOWN',
        )
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 400)

    def test_rejected_when_already_approved(self):
        recording, edar_record = self._make_recording_with_edar(
            status='COMPLETED', review_status='APPROVED', all_known=False,
        )
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_rejected_up_front_when_every_field_already_known(self):
        recording, _ = self._make_recording_with_edar(all_known=True)
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 400)
        # Rejected BEFORE any audio is stored or job created (docs/phase10b-
        # supplemental-audio.md §Reject up front) - not merely a job that later
        # resolves nothing.
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)
        self.assertEqual(ProcessingJob.objects.filter(recording=recording, audio__role='SUPPLEMENTAL').count(), 0)

    def test_recording_in_review_state_is_also_accepted(self):
        recording, _ = self._make_recording_with_edar(status='IN_REVIEW', review_status='IN_REVIEW')
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)

    # --- No client-supplied field list ----------------------------------

    def test_no_field_list_parameter_is_required_or_used(self):
        """The defining Phase 10B constraint (docs/phase10b-supplemental-audio.md
        §API design): even if a client sends fields/target_fields/missing_fields
        in the body, the endpoint ignores them entirely - it only ever accepts
        `audio` and determines eligibility itself."""
        recording, edar_record = self._make_recording_with_edar()
        response = self._put(
            self.owner, recording.recording_id,
            extra_fields={
                'fields': '["case_fir_number"]',
                'target_fields': '["case_fir_number"]',
                'missing_fields': '["case_fir_number"]',
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        job = ProcessingJob.objects.get(recording=recording, audio__role='SUPPLEMENTAL', job_type='STT')
        self.assertEqual(job.status, 'PENDING')

    # --- Side effects of a successful upload ----------------------------

    def test_successful_upload_creates_supplemental_audio_and_pending_stt_job(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)

        supplemental_audios = Audio.objects.filter(recording=recording, role='SUPPLEMENTAL')
        self.assertEqual(supplemental_audios.count(), 1)
        job = ProcessingJob.objects.get(recording=recording, audio=supplemental_audios.first())
        self.assertEqual(job.job_type, 'STT')
        self.assertEqual(job.status, 'PENDING')

    def test_original_audio_and_transcripts_are_untouched(self):
        recording, _ = self._make_recording_with_edar()
        original = Audio.objects.get(recording=recording, role='ORIGINAL')
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)
        original.refresh_from_db()
        self.assertEqual(original.role, 'ORIGINAL')
        self.assertEqual(Audio.objects.filter(recording=recording, role='ORIGINAL').count(), 1)

    def test_recording_status_is_unchanged_by_upload(self):
        recording, _ = self._make_recording_with_edar(status='IN_REVIEW', review_status='IN_REVIEW')
        response = self._put(self.owner, recording.recording_id)
        self.assertEqual(response.status_code, 200, response.data)
        recording.refresh_from_db()
        self.assertEqual(recording.status, 'IN_REVIEW')

    def test_processing_is_asynchronous_not_synchronous(self):
        """No STT/translation/extraction happens inline with the request - the job
        this creates is left PENDING, exactly like the original upload
        (docs/phase1-audio-ingestion.md), never run synchronously here."""
        recording, edar_record = self._make_recording_with_edar()
        self._put(self.owner, recording.recording_id)
        job = ProcessingJob.objects.get(recording=recording, audio__role='SUPPLEMENTAL')
        self.assertEqual(job.status, 'PENDING')
        self.assertIsNone(job.completed_at)
        # The AI field this call could theoretically resolve is still UNKNOWN -
        # nothing was extracted synchronously.
        field = EdarFieldValue.objects.get(edar_record=edar_record, field_key='weather_at_time_of_crash')
        self.assertEqual(field.known, 'UNKNOWN')

    def test_multiple_sequential_supplemental_uploads_are_accepted(self):
        recording, _ = self._make_recording_with_edar()
        first = self._put(self.owner, recording.recording_id)
        self.assertEqual(first.status_code, 200, first.data)
        second = self._put(self.owner, recording.recording_id)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 2)

    def test_response_shape_matches_get_response(self):
        recording, _ = self._make_recording_with_edar()
        put_response = self._put(self.owner, recording.recording_id)
        get_response = self._client_as(self.owner).get(f'/recordings/{recording.recording_id}/')
        self.assertEqual(set(put_response.data['data'].keys()), set(get_response.data['data'].keys()))

    def test_activity_log_records_supplemental_audio_upload(self):
        from csc_apps.activity_log.models import ActivityLog

        recording, _ = self._make_recording_with_edar()
        self._put(self.owner, recording.recording_id)
        entry = ActivityLog.objects.filter(model='Audio', action='Create').order_by('-created_on').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.details.get('role'), 'SUPPLEMENTAL')
        self.assertEqual(entry.details.get('recording_id'), recording.recording_id)

    def test_patch_and_delete_remain_unsupported(self):
        recording, _ = self._make_recording_with_edar()
        client = self._client_as(self.owner)
        for method in ('patch', 'delete'):
            response = getattr(client, method)(f'/recordings/{recording.recording_id}/', {})
            self.assertEqual(response.status_code, 405, method)


class RecordingSupplementAudioAdversarialTests(TestCase):
    """Adversarial regression pass on PUT /recordings/<id>/: validation boundaries,
    storage-failure cleanup, and DEBUG-mode error-message masking - the same class
    of checks Phase 1's own upload endpoint and Phase 9's security suite already
    apply to POST /recordings/ and GET /recordings/<id>/, re-run here because this
    is new code reusing (not necessarily correctly, until verified) those same
    mechanisms."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.owner = User.objects.create_user(
            email='supp-adv-owner@example.com', password='pw', name='Owner', role='OFFICER'
        )

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _original_audio(self, recording):
        storage = LocalPrivateAudioStorage()
        stored = storage.save(recording_id=recording.recording_id, extension='.wav', fileobj=_wav_file())
        return Audio.objects.create(
            recording=recording, role='ORIGINAL', source='UPLOAD', storage_path=stored.storage_path,
            content_type='audio/wav', file_size_bytes=stored.size_bytes, checksum_sha256=stored.checksum_sha256,
        )

    def _make_recording_with_edar(self):
        recording = Recording.objects.create(officer=self.owner, status='READY_FOR_REVIEW')
        self._original_audio(recording)
        edar_record = EdarRecord.objects.create(recording=recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='case_fir_number', layer='AI', known='UNKNOWN',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        return recording, edar_record

    def _put(self, recording_id, audio_fileobj=None, omit_audio=False):
        data = {} if omit_audio else {'audio': audio_fileobj or _wav_file()}
        return self._client_as(self.owner).put(f'/recordings/{recording_id}/', data, format='multipart')

    # --- Validation boundaries (mirrors Phase 1's own upload validation tests) --

    def test_missing_audio_field_is_rejected_and_creates_nothing(self):
        recording, _ = self._make_recording_with_edar()
        response = self._put(recording.recording_id, omit_audio=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)
        self.assertEqual(ProcessingJob.objects.filter(recording=recording, audio__role='SUPPLEMENTAL').count(), 0)

    def test_empty_audio_file_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        recording, _ = self._make_recording_with_edar()
        response = self._put(
            recording.recording_id,
            audio_fileobj=SimpleUploadedFile('empty.wav', b'', content_type='audio/wav'),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_wrong_content_type_is_rejected_and_creates_nothing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        recording, _ = self._make_recording_with_edar()
        response = self._put(
            recording.recording_id,
            audio_fileobj=SimpleUploadedFile('note.txt', b'not audio at all', content_type='text/plain'),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_mislabeled_content_type_with_wrong_magic_bytes_is_rejected(self):
        """Content-Type claims audio/wav but the bytes are not a RIFF/WAVE
        container - the magic-byte cross-check (csc_apps.recordings.validators)
        must catch this the same way it does for the original upload."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        recording, _ = self._make_recording_with_edar()
        response = self._put(
            recording.recording_id,
            audio_fileobj=SimpleUploadedFile('fake.wav', b'NOT A REAL WAV FILE HEADER', content_type='audio/wav'),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_oversized_file_is_rejected_and_creates_nothing(self):
        recording, _ = self._make_recording_with_edar()
        with override_settings(AUDIO_MAX_UPLOAD_SIZE_BYTES=10):
            response = self._put(recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)

    def test_upload_above_djangos_in_memory_threshold_does_not_crash(self):
        """Regression for the Phase 1 fix-pass defect (docs/phase10b-supplemental-
        audio.md, csc/settings.py FILE_UPLOAD_MAX_MEMORY_SIZE): a file above
        Django's 2.5MB default in-memory threshold becomes an on-disk
        TemporaryUploadedFile wrapping a real OS file handle, which
        csc_apps.common.serializer_validations's `data.copy()` cannot deepcopy.
        The original upload endpoint hit an unhandled 500 for this before the
        settings fix; re-verified here on the new PUT endpoint since it goes
        through the identical SerializerValidations decorator."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        recording, _ = self._make_recording_with_edar()
        large_content = b'RIFF' + b'\x00\x00\x00\x00' + b'WAVE' + b'\x00' * (3 * 1024 * 1024)
        response = self._put(
            recording.recording_id,
            audio_fileobj=SimpleUploadedFile('large.wav', large_content, content_type='audio/wav'),
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 1)

    def test_invalid_recording_id_type_does_not_500(self):
        recording, _ = self._make_recording_with_edar()
        response = self._client_as(self.owner).put(
            f'/recordings/{recording.recording_id + 999999}/', {'audio': _wav_file()}, format='multipart',
        )
        self.assertEqual(response.status_code, 400)

    # --- Storage-failure cleanup ------------------------------------------

    def test_unexpected_failure_after_storage_write_deletes_the_orphan_file_and_creates_nothing(self):
        """Mirrors the exact defensive pattern csc_apps.recordings.views.
        RecordingView.upload_extract already uses (try/except around the DB
        writes, storage.delete(stored.storage_path) on any failure) - proves the
        supplemental path actually reuses it correctly, not just superficially."""
        import os
        from unittest.mock import patch

        recording, _ = self._make_recording_with_edar()

        def _files():
            found = []
            for _root, _dirs, files in os.walk(self._tmp.name):
                found.extend(files)
            return sorted(found)

        # The recording's ORIGINAL audio (created by _make_recording_with_edar)
        # already has a file on disk before this PUT - captured here so the
        # assertion below proves no *new* orphan was left behind, not that the
        # storage root is empty (it never is).
        files_before = _files()

        with patch('csc_apps.recordings.views.Audio.objects.create', side_effect=RuntimeError('db write failed')):
            response = self._put(recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Audio.objects.filter(recording=recording, role='SUPPLEMENTAL').count(), 0)
        self.assertEqual(ProcessingJob.objects.filter(recording=recording, audio__role='SUPPLEMENTAL').count(), 0)

        self.assertEqual(
            _files(), files_before,
            'a new orphaned supplemental audio file was left on disk after the DB write failed',
        )

    # --- DEBUG-mode error-message masking (mirrors Phase 9's own pattern) ------

    def test_unexpected_exception_with_debug_false_returns_generic_message(self):
        from unittest.mock import patch

        from csc.config import Configurations

        recording, _ = self._make_recording_with_edar()
        sensitive = 'RuntimeError: connection to internal-db-host:5432 refused, password=hunter2'
        with patch.object(Configurations, 'debug', False):
            with patch('csc_apps.recordings.views.Audio.objects.create', side_effect=RuntimeError(sensitive)):
                response = self._put(recording.recording_id)
        self.assertEqual(response.status_code, 400)
        rendered = str(response.data)
        self.assertNotIn('hunter2', rendered)
        self.assertNotIn('internal-db-host', rendered)
        self.assertNotIn('Traceback', rendered)

    def test_unexpected_exception_with_debug_true_still_has_no_python_traceback_object(self):
        from unittest.mock import patch

        from csc.config import Configurations

        recording, _ = self._make_recording_with_edar()
        with patch.object(Configurations, 'debug', True):
            with patch('csc_apps.recordings.views.Audio.objects.create', side_effect=RuntimeError('boom')):
                response = self._put(recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback (most recent call last)', str(response.data))

    def test_validation_error_message_is_always_shown_verbatim_regardless_of_debug(self):
        """ValueError-raised domain validation messages (e.g. 'already approved',
        'every field already known') are developer-authored and safe by design -
        they must reach the client unchanged whether DEBUG is True or False,
        unlike a genuinely unexpected exception."""
        from unittest.mock import patch

        from csc.config import Configurations

        recording, edar_record = self._make_recording_with_edar()
        EdarFieldValue.objects.filter(edar_record=edar_record).update(known='KNOWN', value='45/2026')
        with patch.object(Configurations, 'debug', False):
            response = self._put(recording.recording_id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('already known', str(response.data))
