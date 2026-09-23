"""Officer review + approval (docs/phase6-officer-review-approval.md). The one place
an APPROVED EdarFieldValue row is ever written - AI rows are never touched here
(FINAL NON-NEGOTIABLE RULE: Gemini AI output is immutable).

Not a background job like csc_apps.processing's STT/translation/extraction services -
approval is a synchronous, officer-triggered write, called directly from
csc_apps.recordings.views inside one database transaction.
"""

import dataclasses

from django.db import transaction
from django.utils import timezone

from csc_apps.activity_log.models import ActivityLog
from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.schema_loader import load_schema
from csc_apps.edar.schema_validation import validate_approved_value
from csc_apps.recordings.state_machine import can_transition, transition


@dataclasses.dataclass
class FieldChange:
    """One field where the approved value/known state differs from the AI
    candidate's - the field-level audit unit (docs/phase6-officer-review-
    approval.md §Audit trail). Not emitted for a field the officer left untouched,
    even if they resubmitted it with the same value."""

    field_key: str
    ai_known: str
    ai_value: object
    approved_known: str
    approved_value: object


@dataclasses.dataclass
class ApprovalResult:
    edar_record: EdarRecord
    approved_field_count: int
    changes: list[FieldChange]


def approve_edar(edar_record: EdarRecord, officer, edits: dict[str, dict]) -> ApprovalResult:
    """`edits` is a partial map of `{field_key: {"known": ..., "value": ...}}` -
    only the fields the officer is changing (source instructions §10/§24: the
    officer submits the human-approved value, not a second copy of every
    untouched field). Every field_key on the current AI candidate that is *not*
    in `edits` is copied through unchanged, so the resulting APPROVED snapshot is
    always the complete logical eDAR dataset (§11), never a partial one.

    Restricted to field_keys the AI candidate already has a row for (KNOWN or
    UNKNOWN) - an officer correcting/confirming what Gemini produced, not adding a
    vehicle/casualty entry the AI never reported (a materially bigger feature,
    deliberately out of Phase 6's scope).

    Raises ValueError (business rules: already approved, no AI candidate, unknown
    field_key) or EdarValidationError (a ValueError subclass: an edit's value
    fails schema validation) - both map through the existing
    csc_apps.common.common.Common.exception_handler's ValueError branch, same as
    every other domain validation failure in this codebase. On any failure, no
    APPROVED row is written and the AI layer is untouched (atomic transaction)."""
    if edar_record.review_status == 'APPROVED':
        raise ValueError('This eDAR record has already been approved')

    ai_rows = list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI'))
    if not ai_rows:
        raise ValueError('No AI eDAR candidate exists for this recording')

    ai_by_key = {row.field_key: row for row in ai_rows}
    unknown_edit_keys = set(edits) - set(ai_by_key)
    if unknown_edit_keys:
        raise ValueError(
            f'Not part of the AI candidate for this recording: {sorted(unknown_edit_keys)}'
        )

    schema = load_schema()
    for field_key, edit in edits.items():
        validate_approved_value(field_key, edit['known'], edit['value'], schema=schema)

    approved_rows: list[EdarFieldValue] = []
    changes: list[FieldChange] = []
    for row in ai_rows:
        edit = edits.get(row.field_key)
        approved_known = edit['known'] if edit is not None else row.known
        approved_value = edit['value'] if edit is not None else row.value
        approved_rows.append(EdarFieldValue(
            edar_record=edar_record, field_key=row.field_key, layer='APPROVED',
            known=approved_known, value=approved_value, updated_by=officer,
        ))
        if approved_known != row.known or approved_value != row.value:
            changes.append(FieldChange(
                field_key=row.field_key, ai_known=row.known, ai_value=row.value,
                approved_known=approved_known, approved_value=approved_value,
            ))

    with transaction.atomic():
        # Defensive, not expected in normal operation (the review_status guard
        # above already prevents a second approval): idempotent replace, same
        # pattern as extraction_service's AI-row replace, so a stray row from an
        # earlier aborted attempt can never coexist with this snapshot.
        EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED').delete()
        EdarFieldValue.objects.bulk_create(approved_rows)

        edar_record.review_status = 'APPROVED'
        edar_record.reviewed_by = officer
        edar_record.reviewed_at = timezone.now()
        edar_record.save(update_fields=['review_status', 'reviewed_by', 'reviewed_at'])

        # Recording-level lifecycle (docs/recording-state-machine.md): reuses the
        # existing READY_FOR_REVIEW -> IN_REVIEW -> COMPLETED path rather than
        # adding a new transition edge. A recording extracted before this state
        # was wired up (or otherwise not yet advanced) is walked forward from
        # wherever it currently is; can_transition guards each hop so this is a
        # no-op for a recording already at IN_REVIEW.
        recording = edar_record.recording
        if can_transition(recording.status, 'READY_FOR_REVIEW'):
            transition(recording, 'READY_FOR_REVIEW')
        if can_transition(recording.status, 'IN_REVIEW'):
            transition(recording, 'IN_REVIEW')
        if can_transition(recording.status, 'COMPLETED'):
            transition(recording, 'COMPLETED')

        ActivityLog.record(
            user=officer, action='Update', model='EdarRecord',
            details={
                'recording_id': edar_record.recording_id,
                'edar_record_id': edar_record.edar_record_id,
                'approved_field_count': len(approved_rows),
                'changed_field_count': len(changes),
                # Field-level change audit (§16) via ActivityLog's existing JSON
                # details mechanism - no second audit system, no AI row rewritten
                # to reconstruct this later. Small officer-submitted values only
                # (never transcript text, never a secret).
                'changed_fields': [
                    {
                        'field': c.field_key, 'aiKnown': c.ai_known, 'aiValue': c.ai_value,
                        'approvedKnown': c.approved_known, 'approvedValue': c.approved_value,
                    }
                    for c in changes
                ],
            },
        )

    return ApprovalResult(edar_record=edar_record, approved_field_count=len(approved_rows), changes=changes)
