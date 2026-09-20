import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from sarvamai.core.api_error import ApiError

from google.genai import errors as genai_errors

from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.schema_loader import load_schema
from csc_apps.processing import event_types
from csc_apps.processing.error_classification import is_retryable
from csc_apps.processing.extraction_service import run_extraction_job
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ExtractionResult, ExtractedField, FieldSource, ProviderError, TranscriptResult, TranslationResult
from csc_apps.processing.providers.gemini.provider import GeminiExtractionProvider
from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys
from csc_apps.processing.providers.sarvam.chunker import MAX_TRANSLATION_INPUT_CHARS, chunk_text
from csc_apps.processing.providers.sarvam.provider import SarvamSpeechToTextProvider
from csc_apps.processing.providers.sarvam.translation_provider import SarvamTranslationProvider
from csc_apps.processing.stt_service import run_stt_job
from csc_apps.processing.tasks.base import TaskEnvelope
from csc_apps.processing.tasks.inline import InlineTaskRunner
from csc_apps.processing.translation_service import run_translation_job
from csc_apps.recordings.models.audio import Audio, Transcript
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.storage import LocalPrivateAudioStorage

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'recordings' / 'tests_fixtures'
_TINY_VALID_WAV = (_FIXTURES_DIR / 'tiny_valid.wav').read_bytes()


class ErrorClassificationTests(SimpleTestCase):
    """docs/error-retry-strategy.md §1 - each error code is either retryable or
    non-retryable, and an unrecognized code must fail loudly rather than defaulting
    silently to either."""

    def test_provider_timeouts_are_retryable(self):
        self.assertTrue(is_retryable('STT_PROVIDER_TIMEOUT'))
        self.assertTrue(is_retryable('TRANSLATION_PROVIDER_UNAVAILABLE'))

    def test_sarvam_rate_limit_is_retryable(self):
        self.assertTrue(is_retryable('STT_PROVIDER_RATE_LIMITED'))

    def test_malformed_extraction_response_is_not_retryable(self):
        self.assertFalse(is_retryable('EXTRACTION_MALFORMED_RESPONSE'))
        self.assertFalse(is_retryable('INVALID_AUDIO'))

    def test_sarvam_auth_and_malformed_and_empty_transcript_are_not_retryable(self):
        self.assertFalse(is_retryable('STT_AUTHENTICATION_FAILED'))
        self.assertFalse(is_retryable('STT_MALFORMED_RESPONSE'))
        self.assertFalse(is_retryable('STT_EMPTY_TRANSCRIPT'))

    def test_unclassified_error_code_raises(self):
        with self.assertRaises(ValueError):
            is_retryable('SOME_UNKNOWN_CODE')


class InlineTaskRunnerTests(SimpleTestCase):
    """docs/processing-pipeline.md §3 - InlineTaskRunner executes synchronously at
    enqueue time; it is the only TaskRunner Phase 0 ships (OD-003)."""

    def test_enqueue_runs_the_task_immediately(self):
        calls = []
        InlineTaskRunner().enqueue(TaskEnvelope(job_id=1, run=lambda: calls.append('ran')))
        self.assertEqual(calls, ['ran'])


class ProcessingJobModelTests(TestCase):
    def setUp(self):
        officer = User.objects.create_user(
            email='officer@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=officer)

    def test_defaults(self):
        job = ProcessingJob.objects.create(recording=self.recording, job_type='STT')
        self.assertEqual(job.status, 'PENDING')
        self.assertEqual(job.attempt_count, 0)
        self.assertEqual(job.max_attempts, 3)


def _fake_sarvam_job(
    transcript_text='namaste, gaadi accident ho gaya',
    language_code='hi-IN',
    request_id='req-abc123',
    job_state='Completed',
    error_message=None,
    file_results=None,
):
    """A MagicMock standing in for sarvamai's SpeechToTextJob handle - download_outputs
    actually writes a real JSON file to the given output_dir, since
    SarvamSpeechToTextProvider reads it back off disk (docs/phase2-sarvam-stt.md
    §Sarvam response mapping)."""
    job = MagicMock()
    job.upload_files.return_value = True

    status = MagicMock()
    status.job_state = job_state
    status.error_message = error_message
    job.wait_until_complete.return_value = status

    job.get_file_results.return_value = file_results or {
        'successful': [{'file_name': 'audio.wav', 'output_file': 'audio.wav.output.json'}],
        'failed': [],
    }

    def _download_outputs(output_dir):
        payload = {'request_id': request_id, 'transcript': transcript_text, 'language_code': language_code}
        with open(os.path.join(output_dir, 'audio.wav.json'), 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return True

    job.download_outputs.side_effect = _download_outputs
    return job


class SarvamSpeechToTextProviderTests(SimpleTestCase):
    """docs/phase2-sarvam-stt.md §Sarvam integration, §Testing (A. Provider) - the
    sarvamai SDK client is fully mocked; no real network call is ever made."""

    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(_TINY_VALID_WAV)
            self.local_audio_path = f.name
        self.addCleanup(lambda: os.path.exists(self.local_audio_path) and os.unlink(self.local_audio_path))

    def _provider(self):
        provider = SarvamSpeechToTextProvider()
        provider._api_key = 'test-key'
        return provider

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_successful_response_maps_to_canonical_transcript_result(self, mock_sarvam_ai_cls):
        mock_client = MagicMock()
        mock_sarvam_ai_cls.return_value = mock_client
        mock_client.speech_to_text_job.create_job.return_value = _fake_sarvam_job(
            transcript_text='namaste, gaadi accident ho gaya', language_code='hi-IN', request_id='req-1'
        )

        result = self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')

        self.assertIsInstance(result, TranscriptResult)
        self.assertEqual(result.text, 'namaste, gaadi accident ho gaya')
        self.assertEqual(result.detected_language_code, 'hi-IN')
        self.assertEqual(result.provider_name, 'sarvam')
        self.assertEqual(result.provider_metadata, {'model': 'saaras:v3', 'request_id': 'req-1'})
        # mode is hardcoded to transcribe - never translate (product decision E).
        _, kwargs = mock_client.speech_to_text_job.create_job.call_args
        self.assertEqual(kwargs['mode'], 'transcribe')
        self.assertFalse(kwargs['with_diarization'])

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_malformed_response_missing_transcript_field_raises(self, mock_sarvam_ai_cls):
        job = _fake_sarvam_job()
        job.download_outputs.side_effect = lambda output_dir: (
            open(os.path.join(output_dir, 'audio.wav.json'), 'w', encoding='utf-8').write('{"request_id": "x"}')
        )
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.return_value = job

        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_MALFORMED_RESPONSE')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_empty_transcript_raises_empty_transcript_error(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.return_value = _fake_sarvam_job(
            transcript_text='   '
        )

        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_EMPTY_TRANSCRIPT')

    def test_missing_api_key_raises_authentication_failed_without_calling_sdk(self):
        provider = SarvamSpeechToTextProvider()
        provider._api_key = ''
        with self.assertRaises(ProviderError) as ctx:
            provider.transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_api_error_403_maps_to_authentication_failed(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.side_effect = ApiError(
            status_code=403, body={'error': {'code': 'invalid_api_key_error', 'message': 'bad key'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_api_error_429_maps_to_rate_limited(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.side_effect = ApiError(
            status_code=429, body={'error': {'code': 'rate_limit_exceeded_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_PROVIDER_RATE_LIMITED')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_api_error_500_and_503_map_to_provider_unavailable(self, mock_sarvam_ai_cls):
        for code in (500, 503):
            mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.side_effect = ApiError(
                status_code=code, body={'error': {'code': 'internal_server_error'}}
            )
            with self.assertRaises(ProviderError) as ctx:
                self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
            self.assertEqual(ctx.exception.error_code, 'STT_PROVIDER_UNAVAILABLE')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_sdk_timeout_error_maps_to_provider_timeout(self, mock_sarvam_ai_cls):
        job = _fake_sarvam_job()
        job.wait_until_complete.side_effect = TimeoutError('did not complete within 600 seconds')
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.return_value = job

        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_PROVIDER_TIMEOUT')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_network_connect_error_maps_to_provider_unavailable(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.side_effect = httpx.ConnectError(
            'connection refused'
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_PROVIDER_UNAVAILABLE')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_network_read_timeout_maps_to_provider_timeout(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.side_effect = httpx.ReadTimeout(
            'read timed out'
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_PROVIDER_TIMEOUT')

    @patch('csc_apps.processing.providers.sarvam.provider.SarvamAI')
    def test_failed_file_result_raises_unsupported_input(self, mock_sarvam_ai_cls):
        job = _fake_sarvam_job(
            file_results={'successful': [], 'failed': [{'file_name': 'audio.wav', 'error_message': 'bad audio'}]}
        )
        mock_sarvam_ai_cls.return_value.speech_to_text_job.create_job.return_value = job

        with self.assertRaises(ProviderError) as ctx:
            self._provider().transcribe(local_audio_path=self.local_audio_path, content_type='audio/wav')
        self.assertEqual(ctx.exception.error_code, 'STT_UNSUPPORTED_INPUT')


class SttServiceTests(TestCase):
    """docs/phase2-sarvam-stt.md §Testing (B. Processing, C. Retry) - end-to-end
    against a real database, with only the SpeechToTextProvider boundary mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._override = override_settings(PRIVATE_STORAGE_ROOT=self._tmp.name)
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.officer = User.objects.create_user(
            email='officer@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=self.officer, status='PROCESSING')

        storage = LocalPrivateAudioStorage()
        stored = storage.save(recording_id=self.recording.recording_id, extension='.wav', fileobj=self._audio_file())
        self.audio = Audio.objects.create(
            recording=self.recording,
            source='UPLOAD',
            storage_path=stored.storage_path,
            content_type='audio/wav',
            file_size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
        )
        self.job = ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='PENDING')

    @staticmethod
    def _audio_file():
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile('statement.wav', _TINY_VALID_WAV, content_type='audio/wav')

    def _mock_provider(self, result=None, error=None):
        provider = MagicMock()
        if error is not None:
            provider.transcribe.side_effect = error
        else:
            provider.transcribe.return_value = result or TranscriptResult(
                text='namaste, gaadi accident ho gaya',
                detected_language_code='hi-IN',
                provider_name='sarvam',
                provider_metadata={'model': 'saaras:v3', 'request_id': 'req-1'},
            )
        return provider

    def test_successful_job_persists_transcript_and_succeeds(self):
        run_stt_job(self.job.job_id, provider=self._mock_provider())

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'SUCCEEDED')
        self.assertIsNotNone(self.job.completed_at)

        transcript = Transcript.objects.get(recording=self.recording, language='ORIGINAL')
        self.assertEqual(transcript.text, 'namaste, gaadi accident ho gaya')
        self.assertEqual(transcript.detected_language_code, 'hi-IN')
        self.assertEqual(transcript.provider_name, 'sarvam')

    def test_provider_receives_a_local_path_not_the_storage_key(self):
        provider = self._mock_provider()
        run_stt_job(self.job.job_id, provider=provider)

        called_path = provider.transcribe.call_args.kwargs['local_audio_path']
        self.assertNotEqual(called_path, self.audio.storage_path)
        self.assertTrue(os.path.isabs(called_path))
        # And it must be cleaned up afterwards - never left behind.
        self.assertFalse(os.path.exists(called_path))

    def test_canonical_stored_audio_is_unchanged_after_a_run(self):
        original_bytes = Path(self._tmp.name, self.audio.storage_path).read_bytes()
        run_stt_job(self.job.job_id, provider=self._mock_provider())
        self.assertEqual(Path(self._tmp.name, self.audio.storage_path).read_bytes(), original_bytes)

    def test_processing_events_recorded_on_success(self):
        run_stt_job(self.job.job_id, provider=self._mock_provider())
        event_type_list = list(
            ProcessingEvent.objects.filter(recording=self.recording, job=self.job).values_list(
                'event_type', flat=True
            )
        )
        self.assertIn(event_types.STT_STARTED, event_type_list)
        self.assertIn(event_types.STT_SUCCEEDED, event_type_list)

    def test_activity_log_created_on_success(self):
        run_stt_job(self.job.job_id, provider=self._mock_provider())
        log = ActivityLog.objects.get(user=self.officer, model='Transcript')
        self.assertEqual(log.action, 'Create')
        self.assertEqual(log.details['recording_id'], self.recording.recording_id)

    def test_retryable_failure_under_budget_is_marked_retrying_not_failed(self):
        run_stt_job(self.job.job_id, provider=self._mock_provider(error=ProviderError('STT_PROVIDER_TIMEOUT', 'x')))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')
        self.assertTrue(self.job.is_retryable)
        self.assertEqual(self.job.attempt_count, 1)
        self.assertIsNone(self.job.completed_at)

        event = ProcessingEvent.objects.get(
            recording=self.recording, job=self.job, event_type=event_types.STT_FAILED
        )
        self.assertEqual(event.metadata['error_code'], 'STT_PROVIDER_TIMEOUT')

    def test_retryable_failure_exhausting_max_attempts_becomes_failed(self):
        self.job.attempt_count = 2
        self.job.max_attempts = 3
        self.job.status = 'RETRYING'
        self.job.save()

        run_stt_job(self.job.job_id, provider=self._mock_provider(error=ProviderError('STT_PROVIDER_TIMEOUT', 'x')))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertEqual(self.job.attempt_count, 3)
        self.assertIsNotNone(self.job.completed_at)

    def test_non_retryable_failure_is_failed_immediately_even_on_first_attempt(self):
        run_stt_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('STT_AUTHENTICATION_FAILED', 'x'))
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertFalse(self.job.is_retryable)
        self.assertEqual(self.job.attempt_count, 1)

    def test_failed_job_creates_no_transcript(self):
        run_stt_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('STT_UNSUPPORTED_INPUT', 'bad audio'))
        )
        self.assertFalse(Transcript.objects.filter(recording=self.recording).exists())

    def test_already_succeeded_job_is_not_rerun(self):
        provider = self._mock_provider()
        run_stt_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.transcribe.call_count, 1)

        run_stt_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.transcribe.call_count, 1, 'a SUCCEEDED job must not be re-executed')

    def test_retry_after_failure_does_not_create_duplicate_transcript(self):
        # First attempt fails retryably...
        run_stt_job(self.job.job_id, provider=self._mock_provider(error=ProviderError('STT_PROVIDER_TIMEOUT', 'x')))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')

        # ...second attempt (same job row - no new ProcessingJob created) succeeds.
        run_stt_job(self.job.job_id, provider=self._mock_provider())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'SUCCEEDED')
        self.assertEqual(self.job.attempt_count, 2)
        self.assertEqual(ProcessingJob.objects.filter(recording=self.recording, job_type='STT').count(), 1)
        self.assertEqual(Transcript.objects.filter(recording=self.recording, language='ORIGINAL').count(), 1)


class TranslationChunkerTests(SimpleTestCase):
    """docs/phase3-sarvam-translation.md §Chunking strategy - a pure function, no
    Sarvam SDK involved. The central invariant checked throughout:
    ``''.join(chunk_text(text)) == text`` - no character lost, duplicated, or
    reordered - and every chunk is <= MAX_TRANSLATION_INPUT_CHARS."""

    def _assert_lossless_and_bounded(self, text, max_chars=MAX_TRANSLATION_INPUT_CHARS):
        chunks = chunk_text(text, max_chars=max_chars)
        self.assertEqual(''.join(chunks), text, 'chunks must reassemble into the exact original text')
        for chunk in chunks:
            self.assertLessEqual(len(chunk), max_chars, f'chunk of length {len(chunk)} exceeds {max_chars}')
        return chunks

    def test_empty_input_returns_no_chunks(self):
        self.assertEqual(chunk_text(''), [])

    def test_short_input_returns_single_chunk(self):
        self.assertEqual(chunk_text('Vehicle hit the divider.'), ['Vehicle hit the divider.'])

    def test_exactly_max_chars_is_a_single_chunk(self):
        text = 'a' * MAX_TRANSLATION_INPUT_CHARS
        chunks = self._assert_lossless_and_bounded(text)
        self.assertEqual(len(chunks), 1)

    def test_one_char_over_max_splits_into_two_chunks(self):
        text = 'word ' * 400 + 'x' * (MAX_TRANSLATION_INPUT_CHARS + 1 - len('word ' * 400))
        self.assertGreater(len(text), MAX_TRANSLATION_INPUT_CHARS)
        chunks = self._assert_lossless_and_bounded(text)
        self.assertGreaterEqual(len(chunks), 2)

    def test_long_paragraph_with_no_sentence_boundaries(self):
        # A single long run of prose-like words with no terminal punctuation at all -
        # forces the sentence/paragraph splitter down to its word-boundary fallback.
        text = ' '.join(f'word{i}' for i in range(600))
        self.assertGreater(len(text), MAX_TRANSLATION_INPUT_CHARS)
        self._assert_lossless_and_bounded(text)

    def test_multiple_paragraphs_are_preserved(self):
        text = ('First paragraph sentence one. Sentence two.\n\n'
                'Second paragraph sentence one. Sentence two.\n\n'
                'Third paragraph.')
        chunks = self._assert_lossless_and_bounded(text)
        # Small input - still fits in one chunk, but must still be lossless.
        self.assertEqual(len(chunks), 1)

    def test_multiple_sentences_split_at_sentence_boundaries_when_possible(self):
        sentence = 'The vehicle was speeding near the intersection. '
        text = sentence * 60  # comfortably over 2000 chars, clean sentence boundaries throughout
        chunks = self._assert_lossless_and_bounded(text)
        self.assertGreater(len(chunks), 1)
        # Every chunk boundary lands right after a sentence terminator + the space
        # that follows it, i.e. no chunk is cut mid-sentence.
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith('. '), f'chunk does not end at a sentence boundary: {chunk[-20:]!r}')

    def test_sentence_boundary_near_the_limit(self):
        # Constructed so the accumulated text sits just under the limit, then one
        # more full sentence would push it over - the chunk must end right there,
        # not overflow into the next sentence.
        sentence = 'Sentence. '
        text = sentence * (MAX_TRANSLATION_INPUT_CHARS // len(sentence) + 5)
        chunks = self._assert_lossless_and_bounded(text)
        self.assertGreater(len(chunks), 1)

    def test_single_sentence_exceeding_the_limit_is_split_safely(self):
        # No sentence-terminal punctuation anywhere - one giant "sentence".
        text = 'word ' * 500
        self.assertGreater(len(text), MAX_TRANSLATION_INPUT_CHARS)
        chunks = self._assert_lossless_and_bounded(text)
        self.assertGreater(len(chunks), 1)

    def test_unicode_indic_text_is_preserved(self):
        text = 'गाड़ी तेज़ रफ़्तार में थी और सड़क के किनारे से टकरा गई। ' * 60
        self.assertGreater(len(text), MAX_TRANSLATION_INPUT_CHARS)
        chunks = self._assert_lossless_and_bounded(text)
        self.assertGreater(len(chunks), 1)
        # Devanagari danda '।' is treated as a sentence terminator.
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.rstrip().endswith('।'), f'chunk did not end at a danda boundary: {chunk[-20:]!r}')

    def test_punctuation_is_preserved_exactly(self):
        text = 'Is this the FIR number: 142/2026? Yes - confirmed! "No injuries," he said.'
        self.assertEqual(chunk_text(text), [text])

    def test_whitespace_is_preserved_exactly(self):
        text = 'Line one.\nLine two.\n\nParagraph two,   with extra spaces.'
        self._assert_lossless_and_bounded(text)

    def test_no_text_loss_across_many_chunks(self):
        text = 'The officer recorded the statement near the highway. ' * 200
        self._assert_lossless_and_bounded(text)

    def test_no_duplicated_text_across_chunks(self):
        text = 'Distinct sentence number {}. '.format('{}')
        text = ''.join(f'Distinct sentence number {i}. ' for i in range(150))
        chunks = self._assert_lossless_and_bounded(text)
        # If any content were duplicated, the reassembled text would be longer than
        # (or different from) the original - already checked by _assert_lossless_and_bounded
        # above; additionally confirm each numbered sentence appears exactly once.
        for i in range(150):
            self.assertEqual(text.count(f'number {i}.'), 1)
            self.assertEqual(''.join(chunks).count(f'number {i}.'), 1)

    def test_chunking_is_deterministic(self):
        text = 'One sentence. ' * 300 + 'गाड़ी दुर्घटना। ' * 100
        self.assertEqual(chunk_text(text), chunk_text(text))

    def test_every_chunk_respects_a_custom_limit(self):
        text = 'Short words here and there. ' * 100
        chunks = self._assert_lossless_and_bounded(text, max_chars=100)
        self.assertGreater(len(chunks), 1)


def _fake_translation_response(translated_text='the vehicle was speeding', source_language_code='hi-IN'):
    response = MagicMock()
    response.translated_text = translated_text
    response.source_language_code = source_language_code
    response.request_id = 'req-translate-1'
    return response


class SarvamTranslationProviderTests(SimpleTestCase):
    """docs/phase3-sarvam-translation.md §Testing (A. Provider) - the sarvamai SDK
    client is fully mocked; no real network call is ever made."""

    def _provider(self):
        provider = SarvamTranslationProvider()
        provider._api_key = 'test-key'
        return provider

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_successful_translation_maps_to_canonical_result(self, mock_sarvam_ai_cls):
        mock_client = MagicMock()
        mock_sarvam_ai_cls.return_value = mock_client
        mock_client.text.translate.return_value = _fake_translation_response(
            translated_text='the vehicle was speeding', source_language_code='hi-IN'
        )

        result = self._provider().translate(
            text='gaadi tez chal rahi thi', source_language_code='hi-IN', target_language_code='en-IN'
        )

        self.assertIsInstance(result, TranslationResult)
        self.assertEqual(result.text, 'the vehicle was speeding')
        self.assertEqual(result.source_language_code, 'hi-IN')
        self.assertEqual(result.target_language_code, 'en-IN')
        self.assertEqual(result.provider_name, 'sarvam')
        self.assertEqual(result.provider_metadata, {'model': 'sarvam-translate:v1', 'chunk_count': 1})

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_correct_source_and_target_language_and_model_are_sent(self, mock_sarvam_ai_cls):
        mock_client = MagicMock()
        mock_sarvam_ai_cls.return_value = mock_client
        mock_client.text.translate.return_value = _fake_translation_response()

        self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')

        _, kwargs = mock_client.text.translate.call_args
        self.assertEqual(kwargs['source_language_code'], 'hi-IN')
        self.assertEqual(kwargs['target_language_code'], 'en-IN')
        self.assertEqual(kwargs['model'], 'sarvam-translate:v1')
        self.assertEqual(kwargs['mode'], 'formal')

    def test_unsupported_source_language_is_rejected_before_calling_sdk(self):
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='auto', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')

    def test_missing_source_language_is_rejected(self):
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code=None, target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_empty_translated_text_raises_empty_output(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.return_value = _fake_translation_response(
            translated_text='   '
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_EMPTY_OUTPUT')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_malformed_response_missing_translated_text_raises(self, mock_sarvam_ai_cls):
        response = MagicMock()
        response.translated_text = None
        mock_sarvam_ai_cls.return_value.text.translate.return_value = response
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_MALFORMED_RESPONSE')

    def test_missing_api_key_raises_authentication_failed_without_calling_sdk(self):
        provider = SarvamTranslationProvider()
        provider._api_key = ''
        with self.assertRaises(ProviderError) as ctx:
            provider.translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_api_error_400_maps_to_unsupported_input(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = ApiError(
            status_code=400, body={'error': {'code': 'invalid_request_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_api_error_403_maps_to_authentication_failed(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = ApiError(
            status_code=403, body={'error': {'code': 'invalid_api_key_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_api_error_422_maps_to_unsupported_input(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = ApiError(
            status_code=422, body={'error': {'code': 'unprocessable_entity_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_api_error_429_maps_to_rate_limited(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = ApiError(
            status_code=429, body={'error': {'code': 'rate_limit_exceeded_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_PROVIDER_RATE_LIMITED')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_api_error_500_maps_to_provider_unavailable(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = ApiError(
            status_code=500, body={'error': {'code': 'internal_server_error'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_PROVIDER_UNAVAILABLE')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_timeout_maps_to_provider_timeout(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = httpx.ReadTimeout('read timed out')
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_PROVIDER_TIMEOUT')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_network_failure_maps_to_provider_unavailable(self, mock_sarvam_ai_cls):
        mock_sarvam_ai_cls.return_value.text.translate.side_effect = httpx.ConnectError('connection refused')
        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text='hello', source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_PROVIDER_UNAVAILABLE')

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_multiple_chunks_are_translated_in_order_and_reassembled(self, mock_sarvam_ai_cls):
        mock_client = MagicMock()
        mock_sarvam_ai_cls.return_value = mock_client
        responses = [
            _fake_translation_response(translated_text='First English chunk. '),
            _fake_translation_response(translated_text='Second English chunk. '),
            _fake_translation_response(translated_text='Third English chunk.'),
        ]
        mock_client.text.translate.side_effect = responses

        long_text = 'First sentence. ' * 200 + 'Second sentence. ' * 200 + 'Third sentence.'
        self.assertGreater(len(chunk_text(long_text)), 2)
        # Force exactly 3 chunks regardless of the real chunker's exact split points,
        # by monkeypatching chunk_text's call indirectly is unnecessary - instead
        # just assert order via call args below using the real chunker's output.
        expected_chunk_count = len(chunk_text(long_text))
        mock_client.text.translate.side_effect = [
            _fake_translation_response(translated_text=f'chunk{i} ') for i in range(expected_chunk_count)
        ]

        result = self._provider().translate(
            text=long_text, source_language_code='hi-IN', target_language_code='en-IN'
        )

        calls = mock_client.text.translate.call_args_list
        self.assertEqual(len(calls), expected_chunk_count)
        for i, call in enumerate(calls):
            self.assertEqual(call.kwargs['input'], chunk_text(long_text)[i])
        self.assertEqual(result.text, ''.join(f'chunk{i} ' for i in range(expected_chunk_count)))

    @patch('csc_apps.processing.providers.sarvam.translation_provider.SarvamAI')
    def test_partial_chunk_failure_raises_without_returning_partial_result(self, mock_sarvam_ai_cls):
        mock_client = MagicMock()
        mock_sarvam_ai_cls.return_value = mock_client
        long_text = 'First sentence. ' * 200 + 'Second sentence. ' * 200 + 'Third sentence.'
        chunk_count = len(chunk_text(long_text))
        self.assertGreaterEqual(chunk_count, 2)

        side_effects = [_fake_translation_response(translated_text='ok ') for _ in range(chunk_count)]
        side_effects[1] = ApiError(status_code=500, body={'error': {'code': 'internal_server_error'}})
        mock_client.text.translate.side_effect = side_effects

        with self.assertRaises(ProviderError) as ctx:
            self._provider().translate(text=long_text, source_language_code='hi-IN', target_language_code='en-IN')
        self.assertEqual(ctx.exception.error_code, 'TRANSLATION_PROVIDER_UNAVAILABLE')


class TranslationServiceTests(TestCase):
    """docs/phase3-sarvam-translation.md §Testing (B. Service) - end-to-end against a
    real database, with only the TranslationProvider boundary mocked. No audio, no
    storage, no SpeechToTextProvider involved anywhere in this class."""

    def setUp(self):
        self.officer = User.objects.create_user(
            email='officer@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=self.officer, status='PROCESSING')
        self.stt_job = ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        self.original_transcript = Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='gaadi tez chal rahi thi aur takra gayi',
            detected_language_code='hi-IN', provider_name='sarvam',
            provider_metadata={'model': 'saaras:v3', 'request_id': 'req-stt-1'},
        )
        self.job = ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='PENDING')

    def _mock_provider(self, result=None, error=None):
        provider = MagicMock()
        if error is not None:
            provider.translate.side_effect = error
        else:
            provider.translate.return_value = result or TranslationResult(
                text='the vehicle was speeding and crashed',
                source_language_code='hi-IN',
                target_language_code='en-IN',
                provider_name='sarvam',
                provider_metadata={'model': 'sarvam-translate:v1', 'chunk_count': 1},
            )
        return provider

    def test_original_transcript_is_retrieved_and_passed_to_provider(self):
        provider = self._mock_provider()
        run_translation_job(self.job.job_id, provider=provider)
        _, kwargs = provider.translate.call_args
        self.assertEqual(kwargs['text'], self.original_transcript.text)

    def test_provider_receives_correct_source_language(self):
        provider = self._mock_provider()
        run_translation_job(self.job.job_id, provider=provider)
        _, kwargs = provider.translate.call_args
        self.assertEqual(kwargs['source_language_code'], 'hi-IN')

    def test_target_language_is_en_in(self):
        provider = self._mock_provider()
        run_translation_job(self.job.job_id, provider=provider)
        _, kwargs = provider.translate.call_args
        self.assertEqual(kwargs['target_language_code'], 'en-IN')

    def test_successful_job_persists_english_transcript_and_succeeds(self):
        run_translation_job(self.job.job_id, provider=self._mock_provider())

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'SUCCEEDED')
        self.assertIsNotNone(self.job.completed_at)

        english = Transcript.objects.get(recording=self.recording, language='ENGLISH')
        self.assertEqual(english.text, 'the vehicle was speeding and crashed')
        # detected_language_code holds the language *this row's text* is written in
        # (en-IN), not what it was translated from - see translation_service.py.
        self.assertEqual(english.detected_language_code, 'en-IN')
        self.assertEqual(english.provider_metadata['source_language_code'], 'hi-IN')
        self.assertEqual(english.provider_name, 'sarvam')

    def test_original_transcript_is_unchanged_after_translation(self):
        original_text_before = self.original_transcript.text
        original_language_before = self.original_transcript.detected_language_code
        run_translation_job(self.job.job_id, provider=self._mock_provider())

        self.original_transcript.refresh_from_db()
        self.assertEqual(self.original_transcript.text, original_text_before)
        self.assertEqual(self.original_transcript.detected_language_code, original_language_before)
        self.assertEqual(self.original_transcript.language, 'ORIGINAL')

    def test_processing_events_recorded_on_success(self):
        run_translation_job(self.job.job_id, provider=self._mock_provider())
        event_type_list = list(
            ProcessingEvent.objects.filter(recording=self.recording, job=self.job).values_list(
                'event_type', flat=True
            )
        )
        self.assertIn(event_types.TRANSLATION_STARTED, event_type_list)
        self.assertIn(event_types.TRANSLATION_SUCCEEDED, event_type_list)

    def test_activity_log_created_on_success(self):
        run_translation_job(self.job.job_id, provider=self._mock_provider())
        log = ActivityLog.objects.filter(user=self.officer, model='Transcript', details__language='ENGLISH').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.action, 'Create')

    def test_retryable_failure_under_budget_is_marked_retrying(self):
        run_translation_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('TRANSLATION_PROVIDER_TIMEOUT', 'x'))
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')
        self.assertTrue(self.job.is_retryable)

    def test_non_retryable_failure_is_failed_immediately(self):
        run_translation_job(
            self.job.job_id,
            provider=self._mock_provider(error=ProviderError('TRANSLATION_AUTHENTICATION_FAILED', 'x')),
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertFalse(self.job.is_retryable)

    def test_retry_does_not_invoke_stt(self):
        provider = self._mock_provider(error=ProviderError('TRANSLATION_PROVIDER_TIMEOUT', 'x'))
        run_translation_job(self.job.job_id, provider=provider)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')

        # The STT job (already SUCCEEDED before this test started) must be completely
        # untouched by a translation retry.
        self.stt_job.refresh_from_db()
        self.assertEqual(self.stt_job.status, 'SUCCEEDED')
        self.assertEqual(ProcessingJob.objects.filter(recording=self.recording, job_type='STT').count(), 1)

    def test_retry_after_failure_does_not_duplicate_english_transcript(self):
        run_translation_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('TRANSLATION_PROVIDER_TIMEOUT', 'x'))
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')

        run_translation_job(self.job.job_id, provider=self._mock_provider())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'SUCCEEDED')
        self.assertEqual(Transcript.objects.filter(recording=self.recording, language='ENGLISH').count(), 1)
        self.assertEqual(ProcessingJob.objects.filter(recording=self.recording, job_type='TRANSLATION').count(), 1)

    def test_partial_translation_failure_creates_no_english_transcript(self):
        # The provider itself never returns a partial result (see
        # SarvamTranslationProviderTests.test_partial_chunk_failure_raises_without_returning_partial_result)
        # - this confirms the service layer's handling of that ProviderError also
        # never persists a canonical (even if partial) English transcript.
        run_translation_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('TRANSLATION_PROVIDER_UNAVAILABLE', 'x'))
        )
        self.assertFalse(Transcript.objects.filter(recording=self.recording, language='ENGLISH').exists())

    def test_already_succeeded_job_is_not_rerun(self):
        provider = self._mock_provider()
        run_translation_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.translate.call_count, 1)

        run_translation_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.translate.call_count, 1, 'a SUCCEEDED job must not be re-executed')

    def test_missing_original_transcript_is_a_non_retryable_failure(self):
        self.original_transcript.delete()
        run_translation_job(self.job.job_id, provider=self._mock_provider())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertEqual(self.job.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')

    def test_source_language_missing_on_transcript_fails_via_real_provider_validation(self):
        self.original_transcript.detected_language_code = None
        self.original_transcript.save()
        # Uses the real SarvamTranslationProvider (client construction never reached,
        # since the language-validation guard fires first) rather than a mock, to
        # exercise the actual end-to-end guard described in
        # docs/phase3-sarvam-translation.md §Source language.
        run_translation_job(self.job.job_id, provider=SarvamTranslationProvider())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertEqual(self.job.error_code, 'TRANSLATION_UNSUPPORTED_INPUT')


def _empty_wrapper():
    return {'value': None, 'confidence': None, 'evidence': None}


def _wrapper(value, confidence=0.9, evidence='supporting text'):
    return {'value': value, 'confidence': confidence, 'evidence': evidence}


def _build_candidate(schema, overrides=None, vehicles=None, casualties=None):
    """A full-shape Gemini candidate (every flat field present, null by default) -
    mirrors exactly what GeminiExtractionProvider._flatten_candidate expects to
    receive back from a real structured-output call."""
    from csc_apps.processing.providers.gemini.schema_adapter import repeating_field_keys

    candidate = {key: _empty_wrapper() for key in flat_field_keys(schema)}
    if overrides:
        for key, wrapper in overrides.items():
            candidate[key] = wrapper

    vehicle_keys = repeating_field_keys(schema, 'vehicle')
    candidate['vehicles'] = [
        {**{k: _empty_wrapper() for k in vehicle_keys}, **(v or {})} for v in (vehicles or [])
    ]
    casualty_keys = repeating_field_keys(schema, 'casualty')
    candidate['casualties'] = [
        {**{k: _empty_wrapper() for k in casualty_keys}, **(c or {})} for c in (casualties or [])
    ]
    return candidate


def _fake_gemini_response(candidate: dict):
    response = MagicMock()
    response.text = json.dumps(candidate)
    return response


class GeminiExtractionProviderTests(SimpleTestCase):
    """docs/phase4-gemini-edar-extraction.md §Testing (§61 Provider) - the
    google-genai SDK client is fully mocked; no real network call is ever made."""

    def setUp(self):
        self.schema = load_schema()

    def _provider(self):
        provider = GeminiExtractionProvider()
        provider._api_key = 'test-key'
        return provider

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_successful_response_maps_to_canonical_extraction_result(self, mock_client_cls):
        candidate = _build_candidate(
            self.schema,
            overrides={'crash_type': _wrapper('rear-end collision', 0.9, 'hit the rear of the car')},
            vehicles=[{'vehicle_type': _wrapper('motorcycle', 0.8, 'the motorcycle')}],
        )
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = _fake_gemini_response(candidate)

        result = self._provider().extract(english_text='the motorcycle hit the rear of the car', schema=self.schema)

        self.assertIsInstance(result, ExtractionResult)
        self.assertEqual(result.provider_name, 'gemini')
        by_field = {f.field: f for f in result.fields}
        self.assertEqual(by_field['crash_type'].value, 'rear-end collision')
        self.assertEqual(by_field['vehicle.1.vehicle_type'].value, 'motorcycle')
        self.assertEqual(result.provider_metadata['vehicle_count'], 1)
        self.assertEqual(result.provider_metadata['casualty_count'], 0)
        self.assertIn('gemini-3.8-flash', result.extraction_version)

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_unsupported_fields_are_omitted_not_fabricated(self, mock_client_cls):
        # docs/phase4-gemini-edar-extraction.md §15 "example of non-invention" - only
        # crash_type is supported by the transcript; weather/lighting/speed_limit
        # must not appear as KNOWN fields.
        candidate = _build_candidate(
            self.schema, overrides={'crash_type': _wrapper('rear-end collision')}
        )
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = _fake_gemini_response(candidate)

        result = self._provider().extract(english_text='the motorcycle hit the rear of the car', schema=self.schema)

        returned_fields = {f.field for f in result.fields}
        self.assertEqual(returned_fields, {'crash_type'})
        self.assertNotIn('weather_at_time_of_crash', returned_fields)
        self.assertNotIn('lighting_condition', returned_fields)
        self.assertNotIn('speed_limit_on_road', returned_fields)

    def test_empty_transcript_rejected_before_calling_sdk(self):
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='   ', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_UNSUPPORTED_INPUT')

    def test_missing_api_key_raises_authentication_failed_without_calling_sdk(self):
        provider = GeminiExtractionProvider()
        provider._api_key = ''
        with self.assertRaises(ProviderError) as ctx:
            provider.extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_malformed_json_response_raises(self, mock_client_cls):
        response = MagicMock()
        response.text = 'not valid json{{{'
        mock_client_cls.return_value.models.generate_content.return_value = response

        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_MALFORMED_RESPONSE')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_non_object_json_response_raises(self, mock_client_cls):
        response = MagicMock()
        response.text = json.dumps(['not', 'an', 'object'])
        mock_client_cls.return_value.models.generate_content.return_value = response

        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_MALFORMED_RESPONSE')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_empty_response_text_raises_empty_output(self, mock_client_cls):
        response = MagicMock()
        response.text = ''
        mock_client_cls.return_value.models.generate_content.return_value = response

        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_EMPTY_OUTPUT')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_api_error_403_maps_to_authentication_failed(self, mock_client_cls):
        mock_client_cls.return_value.models.generate_content.side_effect = genai_errors.ClientError(
            403, {'error': {'message': 'invalid API key', 'status': 'PERMISSION_DENIED'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_AUTHENTICATION_FAILED')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_api_error_429_maps_to_rate_limited(self, mock_client_cls):
        mock_client_cls.return_value.models.generate_content.side_effect = genai_errors.ClientError(
            429, {'error': {'message': 'quota exceeded', 'status': 'RESOURCE_EXHAUSTED'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_PROVIDER_RATE_LIMITED')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_api_error_500_maps_to_provider_unavailable(self, mock_client_cls):
        mock_client_cls.return_value.models.generate_content.side_effect = genai_errors.ServerError(
            500, {'error': {'message': 'internal error', 'status': 'INTERNAL'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_PROVIDER_UNAVAILABLE')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_api_error_400_maps_to_unsupported_input(self, mock_client_cls):
        # e.g. the model/schema combination was rejected as an invalid request - a
        # "model error" in the sense of the request being malformed, not a transient
        # fault.
        mock_client_cls.return_value.models.generate_content.side_effect = genai_errors.ClientError(
            400, {'error': {'message': 'invalid argument', 'status': 'INVALID_ARGUMENT'}}
        )
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_UNSUPPORTED_INPUT')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_timeout_maps_to_provider_timeout(self, mock_client_cls):
        mock_client_cls.return_value.models.generate_content.side_effect = TimeoutError('timed out')
        with self.assertRaises(ProviderError) as ctx:
            self._provider().extract(english_text='hello', schema=self.schema)
        self.assertEqual(ctx.exception.error_code, 'EXTRACTION_PROVIDER_TIMEOUT')

    @patch('csc_apps.processing.providers.gemini.provider.genai.Client')
    def test_network_error_propagates_as_unhandled(self, mock_client_cls):
        # A raw connection failure below the SDK's own APIError handling - not
        # classified by this provider (it only wraps errors.APIError/TimeoutError,
        # docs/phase4-gemini-edar-extraction.md §Error handling); the calling service
        # still fails the job, just via the generic exception path rather than a
        # specific error_code. Documented as a known limitation, not silently
        # swallowed.
        mock_client_cls.return_value.models.generate_content.side_effect = ConnectionError('connection refused')
        with self.assertRaises(ConnectionError):
            self._provider().extract(english_text='hello', schema=self.schema)


class ExtractionServiceTests(TestCase):
    """docs/phase4-gemini-edar-extraction.md §Testing (§62 Extraction) - end-to-end
    against a real database, with only the ExtractionProvider boundary mocked."""

    def setUp(self):
        self.schema = load_schema()
        self.officer = User.objects.create_user(
            email='officer4@example.com', password='pw', name='Officer One', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=self.officer, status='PROCESSING')
        ProcessingJob.objects.create(recording=self.recording, job_type='STT', status='SUCCEEDED')
        self.original_transcript = Transcript.objects.create(
            recording=self.recording, language='ORIGINAL', text='gaadi tez chal rahi thi',
            detected_language_code='hi-IN', provider_name='sarvam',
        )
        ProcessingJob.objects.create(recording=self.recording, job_type='TRANSLATION', status='SUCCEEDED')
        self.english_transcript = Transcript.objects.create(
            recording=self.recording, language='ENGLISH',
            text='The motorcycle hit the rear of the car at the intersection.',
            detected_language_code='en-IN', provider_name='sarvam',
        )
        self.job = ProcessingJob.objects.create(recording=self.recording, job_type='EXTRACTION', status='PENDING')

    def _mock_provider(self, result=None, error=None):
        provider = MagicMock()
        if error is not None:
            provider.extract.side_effect = error
        else:
            provider.extract.return_value = result or ExtractionResult(
                fields=[
                    ExtractedField(
                        field='crash_type', value='rear-end collision', confidence=0.9,
                        source=FieldSource(transcript_segment='hit the rear of the car'),
                    ),
                ],
                provider_name='gemini',
                extraction_version='gemini-3.8-flash/prompt-v1/schema-0.1.0',
                provider_metadata={
                    'model': 'gemini-3.8-flash', 'prompt_version': 'v1', 'schema_version': '0.1.0',
                    'vehicle_count': 0, 'casualty_count': 0,
                },
            )
        return provider

    def test_english_transcript_is_retrieved_and_passed_to_provider(self):
        provider = self._mock_provider()
        run_extraction_job(self.job.job_id, provider=provider)
        _, kwargs = provider.extract.call_args
        self.assertEqual(kwargs['english_text'], self.english_transcript.text)

    def test_schema_is_supplied(self):
        provider = self._mock_provider()
        run_extraction_job(self.job.job_id, provider=provider)
        _, kwargs = provider.extract.call_args
        self.assertEqual(kwargs['schema']['field_count'], 42)

    def test_successful_job_persists_ai_layer_edar_field_values(self):
        run_extraction_job(self.job.job_id, provider=self._mock_provider())

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'SUCCEEDED')

        edar_record = EdarRecord.objects.get(recording=self.recording)
        crash_type_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='crash_type', layer='AI')
        self.assertEqual(crash_type_row.value, 'rear-end collision')
        self.assertEqual(crash_type_row.known, 'KNOWN')
        self.assertEqual(crash_type_row.confidence, 0.9)
        self.assertIn('gemini-3.8-flash', crash_type_row.extraction_version)

    def test_unsupported_fields_persisted_as_unknown_not_absent(self):
        # docs/unknown-data-policy.md §2 - a field Gemini attempted but found no
        # evidence for must be UNKNOWN, not simply have no row at all (which would
        # misrepresent "not yet attempted").
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        edar_record = EdarRecord.objects.get(recording=self.recording)
        weather_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='weather_at_time_of_crash')
        self.assertEqual(weather_row.known, 'UNKNOWN')
        self.assertIsNone(weather_row.value)

    def test_all_28_flat_fields_get_a_row(self):
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        edar_record = EdarRecord.objects.get(recording=self.recording)
        persisted_keys = set(
            EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI').values_list('field_key', flat=True)
        )
        self.assertTrue(set(flat_field_keys(self.schema)).issubset(persisted_keys))

    def test_layer_is_ai_never_approved(self):
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        edar_record = EdarRecord.objects.get(recording=self.recording)
        self.assertFalse(EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED').exists())
        self.assertEqual(edar_record.review_status, 'PENDING_REVIEW')

    def test_original_and_english_transcripts_unchanged_after_extraction(self):
        original_text_before = self.original_transcript.text
        english_text_before = self.english_transcript.text
        run_extraction_job(self.job.job_id, provider=self._mock_provider())

        self.original_transcript.refresh_from_db()
        self.english_transcript.refresh_from_db()
        self.assertEqual(self.original_transcript.text, original_text_before)
        self.assertEqual(self.english_transcript.text, english_text_before)

    def test_processing_events_recorded_on_success(self):
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        event_type_list = list(
            ProcessingEvent.objects.filter(recording=self.recording, job=self.job).values_list(
                'event_type', flat=True
            )
        )
        self.assertIn(event_types.EXTRACTION_STARTED, event_type_list)
        self.assertIn(event_types.EXTRACTION_SUCCEEDED, event_type_list)

    def test_activity_log_created_on_success(self):
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        log = ActivityLog.objects.filter(user=self.officer, model='EdarRecord').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.action, 'Create')

    def test_retryable_failure_under_budget_is_marked_retrying(self):
        run_extraction_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('EXTRACTION_PROVIDER_TIMEOUT', 'x'))
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'RETRYING')

    def test_non_retryable_failure_is_failed_immediately(self):
        run_extraction_job(
            self.job.job_id,
            provider=self._mock_provider(error=ProviderError('EXTRACTION_AUTHENTICATION_FAILED', 'x')),
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')

    def test_extraction_failure_does_not_corrupt_transcripts(self):
        run_extraction_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('EXTRACTION_MALFORMED_RESPONSE', 'x'))
        )
        self.original_transcript.refresh_from_db()
        self.english_transcript.refresh_from_db()
        self.assertTrue(self.original_transcript.text)
        self.assertTrue(self.english_transcript.text)

    def test_retry_does_not_rerun_stt_or_translation(self):
        stt_job = ProcessingJob.objects.get(recording=self.recording, job_type='STT')
        translation_job = ProcessingJob.objects.get(recording=self.recording, job_type='TRANSLATION')

        run_extraction_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('EXTRACTION_PROVIDER_TIMEOUT', 'x'))
        )
        run_extraction_job(self.job.job_id, provider=self._mock_provider())

        stt_job.refresh_from_db()
        translation_job.refresh_from_db()
        self.assertEqual(stt_job.status, 'SUCCEEDED')
        self.assertEqual(translation_job.status, 'SUCCEEDED')
        self.assertEqual(ProcessingJob.objects.filter(recording=self.recording, job_type='STT').count(), 1)
        self.assertEqual(ProcessingJob.objects.filter(recording=self.recording, job_type='TRANSLATION').count(), 1)

    def test_retry_does_not_duplicate_edar_record_or_fields(self):
        run_extraction_job(
            self.job.job_id, provider=self._mock_provider(error=ProviderError('EXTRACTION_PROVIDER_TIMEOUT', 'x'))
        )
        run_extraction_job(self.job.job_id, provider=self._mock_provider())

        self.assertEqual(EdarRecord.objects.filter(recording=self.recording).count(), 1)
        edar_record = EdarRecord.objects.get(recording=self.recording)
        self.assertEqual(
            EdarFieldValue.objects.filter(edar_record=edar_record, field_key='crash_type', layer='AI').count(), 1
        )

    def test_transaction_rollback_leaves_no_partial_edar_record_on_validation_failure(self):
        # A field with an out-of-range confidence fails validate_extraction_entry -
        # the whole candidate must be rejected, not partially persisted.
        bad_provider = self._mock_provider(
            result=ExtractionResult(
                fields=[
                    ExtractedField(
                        field='crash_type', value='rear-end collision', confidence=1.5,
                        source=FieldSource(transcript_segment='hit the rear'),
                    ),
                ],
                provider_name='gemini', extraction_version='gemini-3.8-flash/prompt-v1/schema-0.1.0',
                provider_metadata={'vehicle_count': 0, 'casualty_count': 0},
            )
        )
        run_extraction_job(self.job.job_id, provider=bad_provider)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertEqual(self.job.error_code, 'EXTRACTION_SCHEMA_VALIDATION_FAILED')
        self.assertFalse(EdarFieldValue.objects.filter(edar_record__recording=self.recording).exists())

    def test_already_succeeded_job_is_not_rerun(self):
        provider = self._mock_provider()
        run_extraction_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.extract.call_count, 1)

        run_extraction_job(self.job.job_id, provider=provider)
        self.assertEqual(provider.extract.call_count, 1, 'a SUCCEEDED job must not be re-executed')

    def test_missing_english_transcript_is_a_non_retryable_failure(self):
        self.english_transcript.delete()
        run_extraction_job(self.job.job_id, provider=self._mock_provider())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertEqual(self.job.error_code, 'EXTRACTION_UNSUPPORTED_INPUT')

    def test_vehicle_count_never_exceeds_three_even_if_provider_misbehaves(self):
        # Defense-in-depth (docs/phase4-gemini-edar-extraction.md §19): even if a
        # provider implementation violated the 3-vehicle cap, resolve_field_key's
        # max_repetitions check rejects the 4th vehicle's fields, failing the whole
        # candidate rather than silently persisting an over-limit record.
        misbehaving_provider = self._mock_provider(
            result=ExtractionResult(
                fields=[
                    ExtractedField(
                        field='vehicle.4.vehicle_type', value='truck', confidence=0.8,
                        source=FieldSource(transcript_segment='a fourth truck'),
                    ),
                ],
                provider_name='gemini', extraction_version='gemini-3.8-flash/prompt-v1/schema-0.1.0',
                provider_metadata={'vehicle_count': 4, 'casualty_count': 0},
            )
        )
        run_extraction_job(self.job.job_id, provider=misbehaving_provider)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'FAILED')
        self.assertFalse(EdarFieldValue.objects.filter(edar_record__recording=self.recording).exists())
