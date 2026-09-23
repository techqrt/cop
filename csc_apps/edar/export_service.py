"""Builds the Phase 8 approved-eDAR export (docs/phase8-export.md). Pure, read-only
transformation of already-persisted `layer='APPROVED'` rows into the export's
module-grouped JSON shape - no provider call, no re-validation, no write.

FINAL NON-NEGOTIABLE RULE (same as Phase 6's): only `layer='APPROVED'` is ever read
here. `layer='AI'` never appears in this module at all - not as a fallback, not for
comparison. If an approved value is missing, the export fails; it is never silently
filled from the AI candidate.
"""

import re

from csc_apps.edar.schema_loader import load_schema

# JSON key each non-repeating module exports under - names taken directly from the
# source instructions' own module headings (§11 A-G), not invented here.
_MODULE_EXPORT_KEY = {
    'A': 'crashIdentification',
    'B': 'roadEnvironment',
    'C': 'crashCircumstances',
    'F': 'infrastructureObservations',
    'G': 'officerAssessment',
}

# Device-captured (ADR-012), never sent to Gemini and never given an AI/APPROVED
# EdarFieldValue row - excluded from the eDAR field set here the same way
# csc_apps.processing.providers.gemini.schema_adapter.GPS_FIELD_KEY excludes it from
# extraction. Exported separately, from Recording directly (see views.py) - not an
# AI-to-APPROVED leak, since it never entered that layer system to begin with.
GPS_FIELD_KEY = 'gps_coordinates'

_REPEATING_KEY_RE = re.compile(r'^(vehicle|casualty)\.(\d+)\.(.+)$')


class ExportNotAvailable(ValueError):
    """A recording's eDAR data cannot be exported yet. Subclasses ValueError so it
    flows through the existing csc_apps.common.common.Common.exception_handler
    ValueError branch (HTTP 400) - the same convention every other business-rule
    rejection in this codebase already uses (docs/phase6-officer-review-
    approval.md's 'already approved'/'no AI candidate' errors)."""


def build_export(edar_record) -> dict:
    """`edar_record` may be None (no eDAR extraction exists yet). Raises
    ExportNotAvailable unless review_status is exactly APPROVED and at least one
    APPROVED row actually exists - never falls back to the AI candidate for
    anything."""
    if edar_record is None:
        raise ExportNotAvailable('No eDAR extraction exists for this recording yet')
    if edar_record.review_status != 'APPROVED':
        raise ExportNotAvailable('This recording has not been approved yet')

    from csc_apps.edar.models import EdarFieldValue  # local import - avoids a
    # module-load-order cycle with csc_apps.edar.models importing back here is not
    # actually a risk, but this mirrors the existing local-import convention used
    # elsewhere in this codebase for the same defensive reason.

    approved_rows = list(EdarFieldValue.objects.filter(edar_record=edar_record, layer='APPROVED'))
    if not approved_rows:
        # Should not happen - csc_apps.edar.approval_service always creates one
        # APPROVED row per AI field_key in the same transaction that sets
        # review_status='APPROVED'. Defensive: fail loudly rather than export an
        # empty/corrupt dataset.
        raise ExportNotAvailable('Approved eDAR data is missing or incomplete for this recording')

    by_key = {row.field_key: row for row in approved_rows}
    schema = load_schema()
    modules_by_id = {m['module_id']: m for m in schema['modules']}

    export: dict = {}
    for module_id, export_key in _MODULE_EXPORT_KEY.items():
        fields = {}
        for field_def in modules_by_id[module_id]['fields']:
            key = field_def['field_key']
            if key == GPS_FIELD_KEY:
                continue
            fields[key] = _field_value(by_key.get(key))
        export[export_key] = fields

    export['vehicles'] = _repeating_entities(by_key, modules_by_id['D'])
    export['casualties'] = _repeating_entities(by_key, modules_by_id['E'])
    return export


def _field_value(row) -> dict:
    if row is None:
        # A field_key the AI candidate had no row for at all would mean the AI
        # candidate itself was incomplete, which csc_apps.edar.quality_validation
        # already rejects before persistence - this branch exists only as a
        # structural safety net, not a normal code path.
        return {'known': 'UNKNOWN', 'value': None}
    return {'known': row.known, 'value': row.value}


def _repeating_entities(by_key: dict, module: dict) -> list[dict]:
    """One dict per vehicle/casualty index actually present in the APPROVED layer,
    in index order - never flattened, never re-numbered, never padded with
    fabricated entries."""
    entity = module['repeat_entity']
    indices = set()
    for key in by_key:
        match = _REPEATING_KEY_RE.match(key)
        if match and match.group(1) == entity:
            indices.add(int(match.group(2)))

    field_keys = [f['field_key'] for f in module['fields']]
    return [
        {key: _field_value(by_key.get(f'{entity}.{index}.{key}')) for key in field_keys}
        for index in sorted(indices)
    ]
