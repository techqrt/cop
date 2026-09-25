from django.db import models

from csc_apps.recordings.models.recording import Recording


class Audio(models.Model):
    """Raw evidence (Layer 1, ADR-002) - immutable once stored. `storage_path` is an
    opaque backend-agnostic key, not a public URL (docs/security-baseline.md §3,
    docs/open-decisions.md OD-008).
    """

    SOURCE_CHOICES = [
        ('LIVE', 'Live Recording'),
        ('UPLOAD', 'File Upload'),
    ]

    # ORIGINAL: the one audio row Phase 1 upload always creates. SUPPLEMENTAL: a
    # later, targeted follow-up audio (Phase 10B, docs/phase10b-supplemental-
    # audio.md) accepted only to fill fields the ORIGINAL audio left UNKNOWN - never
    # a replacement for it. A Recording has exactly one ORIGINAL Audio and zero or
    # more SUPPLEMENTAL ones (enforced by application logic, not a DB constraint -
    # same convention as EdarFieldValue's layer/known choices).
    ROLE_CHOICES = [
        ('ORIGINAL', 'Original'),
        ('SUPPLEMENTAL', 'Supplemental'),
    ]

    audio_id = models.AutoField(primary_key=True)
    # Changed from OneToOneField (Phase 10B, docs/phase10b-supplemental-audio.md
    # §Model changes): a Recording can now have more than one Audio row - its
    # original upload plus zero or more later supplemental uploads. `role`
    # distinguishes which is which; nothing about the ORIGINAL row's own identity
    # or immutability changed.
    recording = models.ForeignKey(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='audios'
    )
    role = models.CharField(verbose_name='Role', choices=ROLE_CHOICES, max_length=15, default='ORIGINAL')
    source = models.CharField(verbose_name='Source', choices=SOURCE_CHOICES, max_length=10)
    storage_path = models.CharField(verbose_name='Storage Path', max_length=500)
    content_type = models.CharField(verbose_name='Content Type', max_length=100)
    # Officer-supplied filename at upload time (docs/phase1-audio-ingestion.md §Audio
    # model) - display/audit metadata only, never used to build storage_path (that's
    # always a server-generated key - csc_apps.recordings.storage).
    original_filename = models.CharField(verbose_name='Original Filename', max_length=255, null=True, blank=True)
    duration_seconds = models.FloatField(verbose_name='Duration Seconds', null=True, blank=True)
    file_size_bytes = models.BigIntegerField(verbose_name='File Size Bytes', null=True, blank=True)
    checksum_sha256 = models.CharField(
        verbose_name='Checksum SHA-256', max_length=64, null=True, blank=True, db_index=True
    )
    uploaded_at = models.DateTimeField(verbose_name='Uploaded At', auto_now_add=True)

    class Meta:
        db_table = 'recording_audio'

    def __str__(self) -> str:
        return f'Audio for Recording #{self.recording_id} ({self.source})'


class Transcript(models.Model):
    """Text produced from Audio (docs/domain-model.md §3). LANGUAGE=ENGLISH is always
    derived from LANGUAGE=ORIGINAL via translation, never transcribed independently
    (ADR-004)."""

    LANGUAGE_CHOICES = [
        ('ORIGINAL', 'Original Language'),
        ('ENGLISH', 'English'),
    ]

    transcript_id = models.AutoField(primary_key=True)
    recording = models.ForeignKey(
        verbose_name='Recording', to=Recording, on_delete=models.PROTECT, related_name='transcripts'
    )
    # Which Audio row this transcript was produced from (Phase 10B, docs/phase10b-
    # supplemental-audio.md §Model changes) - nullable only so the migration adding
    # it needs no backfill default; every row this system writes going forward
    # always sets it. Scoping ORIGINAL/ENGLISH uniqueness to `audio` rather than
    # `recording` (see unique_together below) is what lets a supplemental audio get
    # its own ORIGINAL/ENGLISH pair without colliding with the recording's original
    # transcripts.
    audio = models.ForeignKey(
        verbose_name='Audio', to='recordings.Audio', on_delete=models.PROTECT, null=True, blank=True,
        related_name='transcripts',
    )
    language = models.CharField(verbose_name='Language', choices=LANGUAGE_CHOICES, max_length=10)
    text = models.TextField(verbose_name='Text')
    detected_language_code = models.CharField(
        verbose_name='Detected Language Code', max_length=10, null=True, blank=True
    )
    provider_name = models.CharField(verbose_name='Provider Name', max_length=100, null=True, blank=True)
    provider_metadata = models.JSONField(verbose_name='Provider Metadata', default=dict, blank=True)
    created_at = models.DateTimeField(verbose_name='Created At', auto_now_add=True)

    class Meta:
        db_table = 'recording_transcripts'
        unique_together = [('audio', 'language')]

    def __str__(self) -> str:
        return f'{self.language} transcript for Recording #{self.recording_id}'
