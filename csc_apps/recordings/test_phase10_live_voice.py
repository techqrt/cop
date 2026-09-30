"""Phase 10 - Live Voice Agent Integration (docs/phase10-live-voice-agent.md).

Covers: audio-less Recording creation, the `missingFields` addition to the
existing GET /recordings/<id>/ response, and the transcript WebSocket
(WS /recordings/<id>/transcript/) - authentication, authorization, message
validation, incremental extraction reusing the existing eDAR core,
already-KNOWN protection, idempotency, connection lifecycle, and the
approval boundary. Real Sarvam/Gemini calls are mocked throughout except
where noted in the final implementation report's real-provider smoke-test
section.
"""

import asyncio
from unittest.mock import patch

from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from csc.asgi import application
from csc_apps.activity_log.models import ActivityLog
from csc_apps.authentication.models import User
from csc_apps.authentication.views import AuthView
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing.models import ProcessingEvent
from csc_apps.processing.providers.base import ExtractedField, ExtractionResult, FieldSource, ProviderError, \
    TranslationResult
from csc_apps.recordings.models.recording import Recording
from csc_apps.recordings.models.voice_session import VoiceSession


def _extraction_result(*fields, extraction_version='gemini-x/prompt-targeted-v1/schema-0.1.0'):
    return ExtractionResult(
        fields=list(fields), provider_name='gemini', extraction_version=extraction_version, provider_metadata={},
    )


def field(key, value, confidence=0.9, evidence='spoken evidence'):
    return ExtractedField(field=key, value=value, confidence=confidence, source=FieldSource(transcript_segment=evidence))


def _valid_value_for(field_def: dict):
    """A type-appropriate dummy value for `field_def['data_type']`, matching
    csc_apps.edar.schema_validation._validate_value_type's exact rules - used
    only where a test needs every flat field to pass validation (e.g.
    proving the "everything known" completion path), not for fields tested
    individually elsewhere."""
    data_type = field_def['data_type']
    if data_type == 'integer':
        return 1
    if data_type == 'boolean':
        return True
    if data_type == 'date':
        return '2026-05-14'
    if data_type == 'time':
        return '12:00'
    if data_type == 'multi_label_categorical':
        return ['x']
    return 'x'


def _translation(text, source='hi-IN'):
    return TranslationResult(
        text=text, source_language_code=source, target_language_code='en-IN', provider_name='sarvam',
        provider_metadata={},
    )


def _issue_token(user: User) -> str:
    # _issue_token alone only builds the JWT string - a real login
    # (AuthView.login_extract) also persists it to User.access_token, which
    # verify_access_token compares against (single-active-session check,
    # docs/security-baseline.md §1). Without this, every WS connection
    # attempt would fail authentication even with a structurally valid JWT.
    token = AuthView()._issue_token(user)
    User.objects.filter(user_id=user.user_id).update(access_token=token)
    return token


class RecordingLiveVoiceCreationAPITests(TestCase):
    """POST /recordings/ with no `audio` field (docs/phase10-live-voice-agent.md
    §Recording creation). The existing audio-upload path is covered exhaustively
    by RecordingUploadAPITests already - this only proves the new branch."""

    def setUp(self):
        self.officer = User.objects.create_user(
            email='live10-officer@example.com', password='pw', name='Officer', role='OFFICER'
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        return client

    def test_creates_recording_with_no_audio_no_job(self):
        response = self._client().post('/recordings/', {}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data['data']
        self.assertIsNone(data['audioId'])
        self.assertIsNone(data['processingJobId'])
        recording = Recording.objects.get(recording_id=data['recordingId'])
        self.assertEqual(recording.status, 'CREATED')

    def test_gps_and_context_fields_still_accepted(self):
        response = self._client().post(
            '/recordings/',
            {'gps_latitude': 23.03, 'gps_longitude': 72.58, 'road_name': 'SG Highway'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        recording = Recording.objects.get(recording_id=response.data['data']['recordingId'])
        self.assertEqual(recording.road_name, 'SG Highway')

    def test_unauthenticated_request_is_rejected(self):
        response = APIClient().post('/recordings/', {}, format='multipart')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Recording.objects.count(), 0)

    def test_activity_log_records_live_voice_creation(self):
        self._client().post('/recordings/', {}, format='multipart')
        entry = ActivityLog.objects.filter(model='Recording', action='Create').order_by('-created_on').first()
        self.assertEqual(entry.details.get('mode'), 'live_voice')

    def test_existing_audio_upload_path_is_unaffected(self):
        from csc_apps.recordings.tests import _wav_file

        response = self._client().post('/recordings/', {'audio': _wav_file()}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(response.data['data']['audioId'])


class MissingFieldsGetResponseAPITests(TestCase):
    """GET /recordings/<id>/'s new `missingFields` (docs/phase10-live-voice-
    agent.md §GET response, §Missing fields). Applies to every recording with
    an eDAR candidate, upload-driven or voice-driven alike - not a
    voice-only addition."""

    def setUp(self):
        self.officer = User.objects.create_user(
            email='missing10-officer@example.com', password='pw', name='Officer', role='OFFICER'
        )
        self.recording = Recording.objects.create(officer=self.officer, status='RECORDING')

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.officer)
        return client

    def test_empty_before_any_edar_candidate_exists(self):
        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['missingFields'], {})
        self.assertIsNone(response.data['data']['edar'])

    def test_lists_every_unresolved_flat_field_with_a_question_in_schema_order(self):
        from csc_apps.edar.schema_loader import load_schema
        from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys

        edar_record = EdarRecord.objects.create(recording=self.recording)
        keys = flat_field_keys(load_schema())
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key=keys[0], layer='AI', known='KNOWN', value='x',
        )
        for key in keys[1:4]:
            EdarFieldValue.objects.create(edar_record=edar_record, field_key=key, layer='AI', known='UNKNOWN')

        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 200, response.data)
        missing = response.data['data']['missingFields']
        self.assertEqual(list(missing.keys()), keys[1:4])
        self.assertTrue(all(missing.values()))

    def test_empty_once_everything_is_known(self):
        from csc_apps.edar.schema_loader import load_schema
        from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys

        edar_record = EdarRecord.objects.create(recording=self.recording)
        EdarFieldValue.objects.bulk_create([
            EdarFieldValue(edar_record=edar_record, field_key=k, layer='AI', known='KNOWN', value='x')
            for k in flat_field_keys(load_schema())
        ])
        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['missingFields'], {})

    def test_field_names_come_from_canonical_schema_not_invented(self):
        from csc_apps.edar.schema_loader import load_schema
        from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys
        from csc_apps.edar.schema_validation import resolve_field_key

        edar_record = EdarRecord.objects.create(recording=self.recording)
        schema = load_schema()
        key = flat_field_keys(schema)[0]
        EdarFieldValue.objects.create(edar_record=edar_record, field_key=key, layer='AI', known='UNKNOWN')

        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        missing = response.data['data']['missingFields']
        self.assertIn(key, missing)
        # Provably real schema metadata, not an ad-hoc string.
        field_def = resolve_field_key(key, schema=schema)
        self.assertIn(field_def['name'].lower(), missing[key].lower())

    def test_includes_vehicle_and_casualty_fields_once_a_slot_exists(self):
        """A vehicle/casualty slot's fields appear in missingFields once rows
        exist for it - whether created by the batch/audio pipeline's own
        vehicle_count/casualty_count, or by
        csc_apps.recordings.voice_service._bootstrap_entity_fields once
        number_of_vehicles_involved/number_of_persons_involved is KNOWN.
        This test creates the rows directly rather than going through either
        pipeline, since compute_missing_fields only reads existing rows -
        it never invents a slot on its own (2026-09 clarification)."""
        edar_record = EdarRecord.objects.create(recording=self.recording)
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='number_of_vehicles_involved', layer='AI', known='KNOWN', value=2,
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='vehicle.1.vehicle_type', layer='AI', known='UNKNOWN',
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='vehicle.2.vehicle_registration_number', layer='AI', known='UNKNOWN',
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='casualty.1.injury_severity', layer='AI', known='UNKNOWN',
        )

        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        missing = response.data['data']['missingFields']
        self.assertIn('vehicle.1.vehicle_type', missing)
        self.assertIn('vehicle.2.vehicle_registration_number', missing)
        self.assertIn('casualty.1.injury_severity', missing)
        self.assertIn('vehicle 1', missing['vehicle.1.vehicle_type'].lower())
        self.assertIn('vehicle 2', missing['vehicle.2.vehicle_registration_number'].lower())
        self.assertIn('casualty 1', missing['casualty.1.injury_severity'].lower())

    def test_never_invents_a_slot_that_has_no_rows(self):
        """number_of_vehicles_involved says 2, but only vehicle.1.* rows
        exist (e.g. the bootstrap for vehicle 2 hasn't run yet) -
        missingFields must never fabricate vehicle.2.* entries out of the
        count alone."""
        edar_record = EdarRecord.objects.create(recording=self.recording)
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='number_of_vehicles_involved', layer='AI', known='KNOWN', value=2,
        )
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='vehicle.1.vehicle_type', layer='AI', known='UNKNOWN',
        )

        response = self._client().get(f'/recordings/{self.recording.recording_id}/')
        missing = response.data['data']['missingFields']
        self.assertIn('vehicle.1.vehicle_type', missing)
        self.assertFalse(any(k.startswith('vehicle.2.') for k in missing))


def _run(coro):
    return asyncio.run(coro)


class TranscriptWebSocketTests(TransactionTestCase):
    """WS /recordings/<id>/transcript/ (docs/phase10-live-voice-agent.md §5,
    §18, §21). Each test drives the real ASGI application
    (csc.asgi.application) via Channels' own WebsocketCommunicator - not a
    hand-rolled consumer stub.

    TransactionTestCase, not TestCase: the consumer's ORM calls run on a
    different thread than the test method itself (Channels' SyncConsumer
    dispatch), and TestCase's outer-transaction-per-test wrapping deadlocks
    against that on SQLite ("database table is locked") - the standard,
    documented Channels+Django ORM testing pattern, verified directly (this
    class failed with exactly that error under plain TestCase before this
    fix)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email='ws10-owner@example.com', password='pw', name='Owner', role='OFFICER'
        )
        self.other = User.objects.create_user(
            email='ws10-other@example.com', password='pw', name='Other', role='OFFICER'
        )
        self.reviewer = User.objects.create_user(
            email='ws10-reviewer@example.com', password='pw', name='Reviewer', role='REVIEWER'
        )
        self.recording = Recording.objects.create(officer=self.owner, status='CREATED')
        # Issued synchronously here, not inside an `async def run():` closure
        # below - Django's ORM refuses a sync call from an async context
        # (SynchronousOnlyOperation), and _issue_token persists the token to
        # User.access_token (a real DB write).
        self.owner_token = _issue_token(self.owner)
        self.other_token = _issue_token(self.other)
        self.reviewer_token = _issue_token(self.reviewer)

    def _path(self, recording_id=None, token=None):
        recording_id = recording_id if recording_id is not None else self.recording.recording_id
        query = f'?token={token}' if token else ''
        return f'/recordings/{recording_id}/transcript/{query}'

    # --- Authentication / authorization ---------------------------------

    def test_missing_token_is_rejected(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=None))
            connected, _ = await communicator.connect()
            self.assertFalse(connected)
            await communicator.disconnect()
        _run(run())

    def test_invalid_token_is_rejected(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token='not-a-real-token'))
            connected, _ = await communicator.connect()
            self.assertFalse(connected)
            await communicator.disconnect()
        _run(run())

    def test_owner_can_connect(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()
        _run(run())

    def test_reviewer_can_connect(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.reviewer_token))
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()
        _run(run())

    def test_other_officer_is_rejected(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.other_token))
            connected, _ = await communicator.connect()
            self.assertFalse(connected)
            await communicator.disconnect()
        _run(run())
        self.assertEqual(VoiceSession.objects.count(), 0)

    def test_unknown_recording_id_is_rejected(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(recording_id=999999, token=self.owner_token))
            connected, _ = await communicator.connect()
            self.assertFalse(connected)
            await communicator.disconnect()
        _run(run())

    # --- Connection lifecycle -------------------------------------------

    def test_connect_creates_voice_session_and_transitions_recording(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            await communicator.disconnect()
        _run(run())
        session = VoiceSession.objects.get(recording=self.recording)
        self.assertEqual(session.status, 'INCOMPLETE')  # disconnected with nothing resolved yet
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.status, 'RECORDING')

    def test_second_connection_reuses_same_recording_not_a_new_one(self):
        async def run():
            c1 = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await c1.connect()
            await c1.disconnect()
            c2 = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await c2.connect()
            await c2.disconnect()
        _run(run())
        self.assertEqual(VoiceSession.objects.filter(recording=self.recording).count(), 2)
        self.assertEqual(Recording.objects.filter(officer=self.owner).count(), 1)

    # --- Message validation ----------------------------------------------

    def test_missing_transcript_field_is_rejected_with_error_message(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            await communicator.send_json_to({'interactionId': 't1'})
            response = await communicator.receive_json_from()
            self.assertFalse(response['status'])
            await communicator.disconnect()
        _run(run())

    def test_missing_interaction_id_is_rejected(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            await communicator.send_json_to({'transcript': 'hello'})
            response = await communicator.receive_json_from()
            self.assertFalse(response['status'])
            await communicator.disconnect()
        _run(run())

    def test_non_json_message_is_rejected_without_closing_the_connection(self):
        """Regression for a real defect found via live E2E testing (2026-09):
        JsonWebsocketConsumer.receive() calls json.loads() before
        receive_json() ever runs, so a non-JSON text frame raised an uncaught
        JSONDecodeError that closed the connection (code 1011) - ending the
        whole conversation over one bad frame. TranscriptConsumer.receive is
        now overridden to catch this the same way receive_json already
        catches a processing failure."""
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            await communicator.send_to(text_data='not json at all')
            response = await communicator.receive_json_from()
            self.assertFalse(response['status'])
            # the connection must still be open - a second, well-formed
            # message is processed normally afterward.
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result()
                await communicator.send_json_to({'interactionId': 't1', 'transcript': 'hello', 'languageCode': 'en-IN'})
                response2 = await communicator.receive_json_from()
            self.assertTrue(response2['status'])
            await communicator.disconnect()
        _run(run())

    def test_json_that_is_not_an_object_is_rejected_without_closing_the_connection(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            await communicator.send_to(text_data='["not", "an", "object"]')
            response = await communicator.receive_json_from()
            self.assertFalse(response['status'])
            await communicator.disconnect()
        _run(run())

    # --- Incremental extraction, reusing the existing eDAR core ----------

    def test_transcript_resolves_a_flat_field_via_existing_gemini_provider(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                response = await communicator.receive_json_from()
            await communicator.disconnect()
            return response
        response = _run(run())
        self.assertTrue(response['status'])
        self.assertEqual(response['data']['resolvedFieldKeys'], ['road_name'])

        edar_record = EdarRecord.objects.get(recording=self.recording)
        row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='road_name', layer='AI')
        self.assertEqual(row.known, 'KNOWN')
        self.assertEqual(row.value, 'NH 48')

    def test_second_transcript_does_not_overwrite_first_known_field(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE1:
                MockE1.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE2:
                MockE2.return_value.extract.return_value = _extraction_result(
                    field('crash_time', '18:45'), field('road_name', 'SOMETHING ELSE'),
                )
                await communicator.send_json_to({
                    'interactionId': 't2', 'transcript': 'at 6:45pm', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
                called_kwargs = MockE2.return_value.extract.call_args.kwargs
            await communicator.disconnect()
            return called_kwargs
        called_kwargs = _run(run())
        self.assertNotIn('road_name', called_kwargs['target_fields'])

        edar_record = EdarRecord.objects.get(recording=self.recording)
        road_name_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='road_name', layer='AI')
        self.assertEqual(road_name_row.value, 'NH 48')
        crash_time_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='crash_time', layer='AI')
        self.assertEqual(crash_time_row.known, 'KNOWN')

    # --- Vehicle/casualty entity fields, once their count is known --------

    def test_vehicle_fields_become_eligible_the_turn_after_count_is_known(self):
        """Turn 1 resolves number_of_vehicles_involved=2 - vehicle.1.*/
        vehicle.2.* rows should exist (bootstrapped) by the start of turn 2,
        and turn 2 should be able to target and resolve one."""
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE1:
                MockE1.return_value.extract.return_value = _extraction_result(
                    field('number_of_vehicles_involved', 2),
                )
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'two vehicles were involved', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE2:
                MockE2.return_value.extract.return_value = _extraction_result(
                    field('vehicle.1.vehicle_type', 'sedan'),
                )
                await communicator.send_json_to({
                    'interactionId': 't2', 'transcript': 'the first was a sedan', 'languageCode': 'en-IN',
                })
                response2 = await communicator.receive_json_from()
                called_kwargs = MockE2.return_value.extract.call_args.kwargs
            await communicator.disconnect()
            return response2, called_kwargs
        response2, called_kwargs = _run(run())

        # vehicle.1.*/vehicle.2.* were eligible targets for turn 2's call.
        self.assertIn('vehicle.1.vehicle_type', called_kwargs['target_fields'])
        self.assertIn('vehicle.2.vehicle_type', called_kwargs['target_fields'])
        self.assertEqual(response2['data']['resolvedFieldKeys'], ['vehicle.1.vehicle_type'])

        edar_record = EdarRecord.objects.get(recording=self.recording)
        row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='vehicle.1.vehicle_type', layer='AI')
        self.assertEqual(row.value, 'sedan')
        # vehicle.2.* rows exist too (bootstrapped), just still UNKNOWN.
        other = EdarFieldValue.objects.get(
            edar_record=edar_record, field_key='vehicle.2.vehicle_registration_number', layer='AI',
        )
        self.assertEqual(other.known, 'UNKNOWN')

    def test_vehicle_slots_capped_at_schema_max_of_three(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE1:
                MockE1.return_value.extract.return_value = _extraction_result(
                    field('number_of_vehicles_involved', 5),
                )
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'five vehicles were involved', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            # The bootstrap reacts to a count that's already KNOWN at the
            # *start* of a turn - a second turn is what actually triggers it
            # for the count turn 1 itself just resolved.
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE2:
                MockE2.return_value.extract.return_value = _extraction_result()
                await communicator.send_json_to({
                    'interactionId': 't2', 'transcript': 'continuing', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        edar_record = EdarRecord.objects.get(recording=self.recording)
        self.assertTrue(
            EdarFieldValue.objects.filter(edar_record=edar_record, field_key__startswith='vehicle.3.').exists()
        )
        self.assertFalse(
            EdarFieldValue.objects.filter(edar_record=edar_record, field_key__startswith='vehicle.4.').exists()
        )

    def test_casualty_fields_become_eligible_the_turn_after_count_is_known(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE1:
                MockE1.return_value.extract.return_value = _extraction_result(
                    field('number_of_persons_involved', 1),
                )
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'one person was involved', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE2:
                MockE2.return_value.extract.return_value = _extraction_result(
                    field('casualty.1.injury_severity', 'minor injury'),
                )
                await communicator.send_json_to({
                    'interactionId': 't2', 'transcript': 'minor injuries', 'languageCode': 'en-IN',
                })
                response2 = await communicator.receive_json_from()
            await communicator.disconnect()
            return response2
        response2 = _run(run())
        self.assertEqual(response2['data']['resolvedFieldKeys'], ['casualty.1.injury_severity'])

    def test_transcript_resolving_nothing_is_success_not_error(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result()
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'umm', 'languageCode': 'en-IN',
                })
                response = await communicator.receive_json_from()
            await communicator.disconnect()
            return response
        response = _run(run())
        self.assertTrue(response['status'])
        self.assertEqual(response['data']['resolvedFieldKeys'], [])

    def test_provider_error_is_a_valid_no_op_not_a_connection_failure(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.side_effect = ProviderError('EXTRACTION_PROVIDER_UNAVAILABLE', 'down')
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'x', 'languageCode': 'en-IN',
                })
                response = await communicator.receive_json_from()
            await communicator.disconnect()
            return response
        response = _run(run())
        self.assertTrue(response['status'])

    def test_non_english_transcript_is_translated_before_extraction(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.SarvamTranslationProvider') as MockT, \
                 patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockT.return_value.translate.return_value = _translation('on NH 48', source='hi-IN')
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'NH 48 par', 'languageCode': 'hi-IN',
                })
                await communicator.receive_json_from()
                translate_kwargs = MockT.return_value.translate.call_args.kwargs
                extract_kwargs = MockE.return_value.extract.call_args.kwargs
            await communicator.disconnect()
            return translate_kwargs, extract_kwargs
        translate_kwargs, extract_kwargs = _run(run())
        self.assertEqual(translate_kwargs['source_language_code'], 'hi-IN')
        self.assertEqual(extract_kwargs['english_text'], 'on NH 48')

    def test_missing_language_code_is_treated_as_already_english(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.SarvamTranslationProvider') as MockT, \
                 patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({'interactionId': 't1', 'transcript': 'on NH 48'})
                await communicator.receive_json_from()
                translate_called = MockT.return_value.translate.called
                extract_kwargs = MockE.return_value.extract.call_args.kwargs
            await communicator.disconnect()
            return translate_called, extract_kwargs
        translate_called, extract_kwargs = _run(run())
        self.assertFalse(translate_called)
        self.assertEqual(extract_kwargs['english_text'], 'on NH 48')

    # --- Idempotency ------------------------------------------------------

    def test_repeated_interaction_id_is_idempotent(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 'dup-1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                first = await communicator.receive_json_from()
                await communicator.send_json_to({
                    'interactionId': 'dup-1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                second = await communicator.receive_json_from()
                call_count = MockE.return_value.extract.call_count
            await communicator.disconnect()
            return first, second, call_count
        first, second, call_count = _run(run())
        self.assertEqual(first, second)
        self.assertEqual(call_count, 1)

    # --- Approval boundary --------------------------------------------

    def test_never_writes_approved_layer(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        edar_record = EdarRecord.objects.get(recording=self.recording)
        self.assertEqual(EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED').count(), 0)

    def test_transcript_against_an_approved_record_resolves_nothing(self):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='APPROVED')
        EdarFieldValue.objects.create(edar_record=edar_record, field_key='road_name', layer='AI', known='UNKNOWN')

        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                response = await communicator.receive_json_from()
            await communicator.disconnect()
            return response
        response = _run(run())
        self.assertTrue(response['status'])
        edar_record.refresh_from_db()
        road_name_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='road_name', layer='AI')
        self.assertEqual(road_name_row.known, 'UNKNOWN')

    # --- Session lifecycle / audit ---------------------------------------

    def test_disconnect_marks_completed_when_everything_known(self):
        from csc_apps.edar.schema_loader import load_schema
        from csc_apps.edar.schema_validation import resolve_field_key
        from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys

        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                schema = load_schema()
                all_fields = flat_field_keys(schema)
                MockE.return_value.extract.return_value = _extraction_result(
                    *[field(k, _valid_value_for(resolve_field_key(k, schema=schema))) for k in all_fields]
                )
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'everything', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        session = VoiceSession.objects.get(recording=self.recording)
        self.assertEqual(session.status, 'COMPLETED')

    def test_transcripts_accumulate_across_messages(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result()
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'first statement', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
                await communicator.send_json_to({
                    'interactionId': 't2', 'transcript': 'second statement', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        session = VoiceSession.objects.get(recording=self.recording)
        self.assertIn('first statement', session.transcript_original)
        self.assertIn('second statement', session.transcript_original)

    def test_processing_event_and_activity_log_recorded(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'on NH 48', 'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        event = ProcessingEvent.objects.get(recording=self.recording, event_type='voice_turn_processed')
        self.assertEqual(event.metadata['interaction_id'], 't1')
        self.assertEqual(event.metadata['resolved_field_keys'], ['road_name'])
        self.assertTrue(
            ActivityLog.objects.filter(model='EdarRecord', details__event='voice_turn').exists()
        )

    def test_no_secret_or_transcript_content_leaks_in_processing_event(self):
        async def run():
            communicator = WebsocketCommunicator(application, self._path(token=self.owner_token))
            await communicator.connect()
            with patch('csc_apps.recordings.voice_service.GeminiExtractionProvider') as MockE:
                MockE.return_value.extract.return_value = _extraction_result(field('road_name', 'NH 48'))
                await communicator.send_json_to({
                    'interactionId': 't1', 'transcript': 'a very specific secret-sounding sentence XYZ123',
                    'languageCode': 'en-IN',
                })
                await communicator.receive_json_from()
            await communicator.disconnect()
        _run(run())
        entry = ActivityLog.objects.get(model='EdarRecord', details__event='voice_turn')
        self.assertNotIn('XYZ123', str(entry.details))
