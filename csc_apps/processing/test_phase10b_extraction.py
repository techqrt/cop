"""Targeted/supplemental extraction merge mechanics
(docs/phase10b-supplemental-audio.md §Targeted extraction, §Merge). Exercises
csc_apps.processing.extraction_service._run_targeted_extraction directly through
run_extraction_job, dispatched via ProcessingJob.audio.role == 'SUPPLEMENTAL' - the
same public entry point process_pending_extraction_jobs uses. The HTTP surface
(PUT /recordings/<id>/) is covered separately in
csc_apps.recordings.test_phase10b_supplemental_audio.
"""

from csc_apps.authentication.models import User
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.processing.extraction_service import _record_targeted_success, run_extraction_job
from csc_apps.processing.models import ProcessingEvent, ProcessingJob
from csc_apps.processing.providers.base import ExtractedField, ExtractionResult, FieldSource
from csc_apps.processing.test_phase5_extraction import Phase5Base, field, provider_returning, result
from csc_apps.recordings.models.audio import Audio, Transcript


class TargetedExtractionTests(Phase5Base):
    """Reuses Phase5Base's officer/recording/original-pipeline setup (STT+
    TRANSLATION already SUCCEEDED, self.english the ORIGINAL audio's English
    transcript) and adds an already-extracted AI candidate plus a second,
    SUPPLEMENTAL audio+transcript+EXTRACTION job on top of it."""

    def setUp(self):
        super().setUp()
        self.edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        self.known_row = EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='road_name', layer='AI', known='KNOWN',
            value='NH 48', confidence=0.91, source_transcript_segment='on NH 48',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        self.unknown_fir = EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='case_fir_number', layer='AI', known='UNKNOWN',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        self.unknown_weather = EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='weather_at_time_of_crash', layer='AI', known='UNKNOWN',
            extraction_version='gemini-3.8-flash/prompt-v2/schema-0.1.0',
        )
        # An APPROVED-layer row for the same field_key as the AI known row - proves
        # a supplemental merge never touches the APPROVED layer at all.
        self.approved_road_name = EdarFieldValue.objects.create(
            edar_record=self.edar_record, field_key='road_name', layer='APPROVED', known='KNOWN', value='NH 48',
        )

        self.supplemental_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp/audio.wav',
            content_type='audio/wav',
        )
        self.supp_english = Transcript.objects.create(
            recording=self.recording, audio=self.supplemental_audio, language='ENGLISH',
            text='The case number is FIR 45 of 2026 and it was raining heavily.',
            detected_language_code='en-IN', provider_name='identity',
        )
        self.targeted_job = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=self.supplemental_audio, status='PENDING',
        )

    def run_targeted(self, res):
        run_extraction_job(self.targeted_job.job_id, provider=provider_returning(res))
        self.targeted_job.refresh_from_db()
        return self.targeted_job

    # --- Merge correctness -------------------------------------------------

    def test_resolves_only_eligible_fields(self):
        res = ExtractionResult(
            fields=[
                field('case_fir_number', 'FIR 45/2026', evidence='FIR 45 of 2026'),
                field('weather_at_time_of_crash', 'Rain', evidence='raining heavily'),
            ],
            provider_name='gemini', extraction_version='gemini-3.8-flash/prompt-targeted-v1/schema-0.1.0',
            provider_metadata={'call_count': 1, 'target_field_count': 2},
        )
        job = self.run_targeted(res)
        self.assertEqual(job.status, 'SUCCEEDED')

        self.unknown_fir.refresh_from_db()
        self.unknown_weather.refresh_from_db()
        self.assertEqual(self.unknown_fir.known, 'KNOWN')
        self.assertEqual(self.unknown_fir.value, 'FIR 45/2026')
        self.assertEqual(self.unknown_weather.known, 'KNOWN')
        self.assertEqual(self.unknown_weather.value, 'Rain')
        # Provenance shows the targeted prompt version, distinct from the normal
        # extraction path's.
        self.assertIn('targeted', self.unknown_fir.extraction_version)

    def test_already_known_ai_field_is_never_touched(self):
        before = (self.known_row.value, self.known_row.confidence, self.known_row.source_transcript_segment,
                   self.known_row.extraction_version)
        res = result(field('case_fir_number', 'FIR 45/2026'))
        self.run_targeted(res)
        self.known_row.refresh_from_db()
        after = (self.known_row.value, self.known_row.confidence, self.known_row.source_transcript_segment,
                 self.known_row.extraction_version)
        self.assertEqual(before, after)

    def test_approved_layer_is_never_touched(self):
        res = result(field('case_fir_number', 'FIR 45/2026'))
        self.run_targeted(res)
        self.approved_road_name.refresh_from_db()
        self.assertEqual(self.approved_road_name.value, 'NH 48')
        self.assertEqual(self.approved_road_name.known, 'KNOWN')

    def test_no_second_edar_record_or_recording_is_created(self):
        res = result(field('case_fir_number', 'FIR 45/2026'))
        self.run_targeted(res)
        self.assertEqual(EdarRecord.objects.filter(recording=self.recording).count(), 1)

    def test_provider_field_outside_eligible_set_is_discarded(self):
        """Even if Gemini's targeted call somehow returns a field outside what was
        actually asked for, it must never be persisted (docs/phase10b-supplemental-
        audio.md §Server-side enforcement)."""
        res = result(
            field('case_fir_number', 'FIR 45/2026'),
            field('road_name', 'A FABRICATED ROAD NAME'),
        )
        self.run_targeted(res)
        self.known_row.refresh_from_db()
        self.assertEqual(self.known_row.value, 'NH 48')

    def test_partial_resolution_is_success_not_error(self):
        """Resolving only one of two eligible fields (the other stays UNKNOWN) is a
        normal, valid outcome - not a failure."""
        res = result(field('case_fir_number', 'FIR 45/2026'))
        job = self.run_targeted(res)
        self.assertEqual(job.status, 'SUCCEEDED')
        self.unknown_weather.refresh_from_db()
        self.assertEqual(self.unknown_weather.known, 'UNKNOWN')
        self.assertIsNone(self.unknown_weather.value)

    def test_zero_fields_resolved_by_provider_is_success_not_error(self):
        """The provider ran and genuinely found nothing new - a valid, non-error,
        no-fabrication outcome (docs/phase10b-supplemental-audio.md §No
        fabrication)."""
        job = self.run_targeted(result())
        self.assertEqual(job.status, 'SUCCEEDED')
        self.unknown_fir.refresh_from_db()
        self.unknown_weather.refresh_from_db()
        self.assertEqual(self.unknown_fir.known, 'UNKNOWN')
        self.assertEqual(self.unknown_weather.known, 'UNKNOWN')

    def test_zero_eligible_fields_at_merge_time_skips_the_provider_call(self):
        """Race safety (docs/phase10b-supplemental-audio.md §Race safety): if
        every field this job could have targeted was resolved by something else
        before this job ran, it succeeds trivially without ever calling the
        provider."""
        self.unknown_fir.known, self.unknown_fir.value = 'KNOWN', 'FIR 1/2026'
        self.unknown_fir.save()
        self.unknown_weather.known, self.unknown_weather.value = 'KNOWN', 'Clear'
        self.unknown_weather.save()

        provider = provider_returning(result())
        run_extraction_job(self.targeted_job.job_id, provider=provider)
        self.targeted_job.refresh_from_db()

        self.assertEqual(self.targeted_job.status, 'SUCCEEDED')
        provider.extract.assert_not_called()

    def test_invalid_extraction_fails_job_and_leaves_ai_layer_untouched(self):
        bad = ExtractedField(
            field='case_fir_number', value=12345, confidence=0.8,
            source=FieldSource(transcript_segment='FIR 45 of 2026'),
        )
        res = result(bad)
        job = self.run_targeted(res)
        self.assertEqual(job.status, 'FAILED')
        self.assertEqual(job.error_code, 'EXTRACTION_SCHEMA_VALIDATION_FAILED')
        self.unknown_fir.refresh_from_db()
        self.assertEqual(self.unknown_fir.known, 'UNKNOWN')

    def test_processing_events_recorded_for_supplemental_success(self):
        res = result(field('case_fir_number', 'FIR 45/2026'))
        self.run_targeted(res)
        event_types_seen = list(
            ProcessingEvent.objects.filter(job=self.targeted_job).values_list('event_type', flat=True)
        )
        self.assertIn('extraction_started', event_types_seen)
        self.assertIn('quality_validation_succeeded', event_types_seen)
        self.assertIn('extraction_succeeded', event_types_seen)
        succeeded_event = ProcessingEvent.objects.get(job=self.targeted_job, event_type='extraction_succeeded')
        self.assertEqual(succeeded_event.metadata['mode'], 'supplemental')
        self.assertEqual(succeeded_event.metadata['resolved_field_keys'], ['case_fir_number'])

    def test_quality_report_warnings_for_untouched_fields_are_preserved(self):
        """A pre-existing warning on a field this merge never touches must survive
        the quality-summary refresh untouched (docs/phase10b-supplemental-audio.md
        §Quality summary refresh)."""
        self.edar_record.quality_status = 'VALIDATION_WARNING'
        self.edar_record.quality_report = {
            'status': 'VALIDATION_WARNING',
            'errors': [],
            'warnings': [{'field': 'road_name', 'code': 'low_confidence', 'severity': 'warning', 'message': 'x'}],
            'metrics': {},
            'evidenceMatchRule': 'x',
        }
        self.edar_record.save()

        res = result(field('case_fir_number', 'FIR 45/2026'))
        self.run_targeted(res)

        self.edar_record.refresh_from_db()
        fields_with_warnings = {w['field'] for w in self.edar_record.quality_report['warnings']}
        self.assertIn('road_name', fields_with_warnings)

    def test_second_supplemental_upload_only_targets_still_unresolved_fields(self):
        """Sequential supplemental uploads accumulate fixes - a second one only
        ever targets whatever the first left unresolved."""
        first = self.run_targeted(result(field('case_fir_number', 'FIR 45/2026')))
        self.assertEqual(first.status, 'SUCCEEDED')

        second_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp2/audio.wav',
            content_type='audio/wav',
        )
        Transcript.objects.create(
            recording=self.recording, audio=second_audio, language='ENGLISH',
            text='It was raining heavily at the time.', detected_language_code='en-IN', provider_name='identity',
        )
        second_job = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=second_audio, status='PENDING',
        )
        provider = provider_returning(result(field('weather_at_time_of_crash', 'Rain')))
        run_extraction_job(second_job.job_id, provider=provider)
        second_job.refresh_from_db()

        self.assertEqual(second_job.status, 'SUCCEEDED')
        called_kwargs = provider.extract.call_args.kwargs
        self.assertEqual(called_kwargs['target_fields'], ['weather_at_time_of_crash'])

        self.unknown_weather.refresh_from_db()
        self.assertEqual(self.unknown_weather.known, 'KNOWN')
        self.assertEqual(self.unknown_weather.value, 'Rain')

    def test_concurrent_overlapping_merge_does_not_clobber_or_double_report(self):
        """Simulates the genuine interleaving a sequential test can't reach: two
        supplemental jobs both read the AI layer while `case_fir_number` was still
        UNKNOWN, both got a provider result resolving it (slightly differently -
        real providers are not deterministic across two separate audios), and
        job A's merge already committed by the time job B's merge runs. Directly
        exercises _record_targeted_success's own select_for_update + `known ==
        'KNOWN'` recheck (extraction_service.py) - the actual write-time guard,
        not just the read-time eligible_keys recompute, which is unimplemented at
        the assess/resolution stage and does not by itself prevent this."""
        job_a_rows = [{
            'field_key': 'case_fir_number', 'known': 'KNOWN', 'value': 'FIR 45/2026', 'confidence': 0.95,
            'source_transcript_segment': 'FIR 45 of 2026', 'source_start_time': None, 'source_end_time': None,
        }]
        job_b_rows = [{
            'field_key': 'case_fir_number', 'known': 'KNOWN', 'value': 'FIR forty-five slash 2026',
            'confidence': 0.6, 'source_transcript_segment': 'FIR forty five 2026', 'source_start_time': None,
            'source_end_time': None,
        }]

        second_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp2/audio.wav',
            content_type='audio/wav',
        )
        job_b = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=second_audio, status='RUNNING',
        )

        # Job A commits first.
        _record_targeted_success(
            self.targeted_job, self.recording, self.edar_record, resolved_rows=job_a_rows,
            requested_field_count=1, duration_seconds=1.0, extraction_version='gemini/prompt-targeted-v1/schema-x',
        )
        self.unknown_fir.refresh_from_db()
        self.assertEqual(self.unknown_fir.value, 'FIR 45/2026')

        # Job B commits second, with a resolution it computed independently
        # (before A's write) for the SAME field.
        _record_targeted_success(
            job_b, self.recording, self.edar_record, resolved_rows=job_b_rows,
            requested_field_count=1, duration_seconds=1.0, extraction_version='gemini/prompt-targeted-v1/schema-x',
        )
        job_b.refresh_from_db()

        # Job A's value must survive untouched - not overwritten by B's stale one.
        self.unknown_fir.refresh_from_db()
        self.assertEqual(self.unknown_fir.value, 'FIR 45/2026')
        self.assertEqual(self.unknown_fir.extraction_version, 'gemini/prompt-targeted-v1/schema-x')

        # Job B itself must succeed (this is not an error - it did real, valid
        # work, just on a field someone else got to first) and must honestly
        # report that it resolved nothing, not falsely claim the field.
        self.assertEqual(job_b.status, 'SUCCEEDED')
        job_b_event = ProcessingEvent.objects.get(job=job_b, event_type='extraction_succeeded')
        self.assertEqual(job_b_event.metadata['resolved_field_count'], 0)
        self.assertEqual(job_b_event.metadata['resolved_field_keys'], [])


class TargetedExtractionMissingPrerequisiteTests(Phase5Base):
    def test_missing_english_transcript_for_supplemental_audio_fails_non_retryably(self):
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='case_fir_number', layer='AI', known='UNKNOWN',
        )
        supplemental_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp/audio.wav',
            content_type='audio/wav',
        )
        job = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=supplemental_audio, status='PENDING',
        )
        run_extraction_job(job.job_id, provider=provider_returning(result()))
        job.refresh_from_db()
        self.assertEqual(job.status, 'FAILED')
        self.assertEqual(job.error_code, 'EXTRACTION_UNSUPPORTED_INPUT')

    def test_approval_between_upload_and_job_run_blocks_the_merge(self):
        """Adversarial regression: the PUT endpoint rejects a supplemental upload
        once review_status is APPROVED (docs/phase10b-supplemental-audio.md §3),
        but that check only runs at upload time. If the officer approves the
        recording AFTER a supplemental job was queued but BEFORE it actually runs
        (a real, reachable race - approval and the job runner are two independent
        processes with no coordination), the job must not be allowed to silently
        mutate the AI layer of an already-approved record."""
        edar_record = EdarRecord.objects.create(recording=self.recording, review_status='PENDING_REVIEW')
        EdarFieldValue.objects.create(
            edar_record=edar_record, field_key='case_fir_number', layer='AI', known='UNKNOWN',
        )
        supplemental_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp/audio.wav',
            content_type='audio/wav',
        )
        Transcript.objects.create(
            recording=self.recording, audio=supplemental_audio, language='ENGLISH',
            text='The case number is FIR 45 of 2026.', detected_language_code='en-IN', provider_name='identity',
        )
        job = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=supplemental_audio, status='PENDING',
        )

        # The race: approval lands (via the real approval_service, same as the
        # HTTP approve endpoint would trigger) while the supplemental job is still
        # PENDING - a window process_pending_extraction_jobs cannot see or avoid.
        from csc_apps.edar import approval_service

        approval_service.approve_edar(edar_record=edar_record, officer=self.officer, edits={})
        edar_record.refresh_from_db()
        self.assertEqual(edar_record.review_status, 'APPROVED')

        run_extraction_job(job.job_id, provider=provider_returning(result(field('case_fir_number', 'FIR 45/2026'))))
        job.refresh_from_db()

        # Must be refused, not silently merged - the AI row must stay exactly as
        # it was at approval time.
        self.assertEqual(job.status, 'FAILED')
        ai_row = EdarFieldValue.objects.get(edar_record=edar_record, field_key='case_fir_number', layer='AI')
        self.assertEqual(ai_row.known, 'UNKNOWN')

    def test_missing_edar_record_fails_non_retryably(self):
        supplemental_audio = Audio.objects.create(
            recording=self.recording, role='SUPPLEMENTAL', source='UPLOAD', storage_path='supp/audio.wav',
            content_type='audio/wav',
        )
        Transcript.objects.create(
            recording=self.recording, audio=supplemental_audio, language='ENGLISH',
            text='Some supplemental statement.', detected_language_code='en-IN', provider_name='identity',
        )
        job = ProcessingJob.objects.create(
            recording=self.recording, job_type='EXTRACTION', audio=supplemental_audio, status='PENDING',
        )
        run_extraction_job(job.job_id, provider=provider_returning(result()))
        job.refresh_from_db()
        self.assertEqual(job.status, 'FAILED')
        self.assertEqual(job.error_code, 'EXTRACTION_UNSUPPORTED_INPUT')
