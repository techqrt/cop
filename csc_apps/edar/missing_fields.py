"""Canonical eDAR missing-field/question computation (docs/phase10-live-voice-
agent.md §Missing fields, §Missing field -> question mapping). One place -
reused by GET /recordings/<id>/'s `missingFields` (what Sarvam/Flutter uses to
keep a live voice conversation going) - never a second, independently-
maintained field list or question-text source.

Deliberately backend-authoritative, per the canonical 42-field schema (task
§9): never derived from Flutter labels, Sarvam's own field checklist, or
Gemini's response shape.
"""

from csc_apps.edar.models import EdarFieldValue, EdarRecord
from csc_apps.edar.schema_loader import load_schema
from csc_apps.edar.schema_validation import resolve_field_key
from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys


def compute_missing_fields(edar_record: EdarRecord) -> dict[str, str]:
    """Every currently-unresolved flat (non-repeating) canonical eDAR field
    for this record, mapped to its question, in the schema's own
    field-declaration order - deterministic (task §10), never left to the
    conversational agent to decide which is "next". Scoped to the same 28
    flat fields the voice pipeline incrementally extracts
    (csc_apps.recordings.voice_service) - vehicle/casualty (Module D/E)
    fields are out of scope, a documented limitation, not silently dropped.

    A dict keyed by field_key (not a list of {field, question} objects) -
    matches this feature's own task prompt's literal contract; Python dicts
    (and DRF's JSON rendering of them) preserve insertion order, so schema
    order survives into the response the same way it would in a list.
    """
    schema = load_schema()
    unknown_keys = set(
        EdarFieldValue.objects.filter(edar_record=edar_record, layer='AI')
        .exclude(known='KNOWN')
        .values_list('field_key', flat=True)
    )
    if not unknown_keys:
        return {}

    missing = {}
    for key in flat_field_keys(schema):
        if key in unknown_keys:
            field_def = resolve_field_key(key, schema=schema)
            missing[key] = _question_text(field_def)
    return missing


def _question_text(field_def: dict) -> str:
    """Plain English prompt text built from the schema's own field name.
    Sarvam/Flutter owns the conversation (task §11) and is free to rephrase
    or localize this before speaking it - Django's job is only to say which
    field is missing, not to produce final conversational copy."""
    return f"What was the {field_def['name'].lower()}?"
