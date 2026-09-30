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
from csc_apps.processing.providers.gemini.schema_adapter import flat_field_keys, repeating_field_keys


def compute_missing_fields(edar_record: EdarRecord) -> dict[str, str]:
    """Every currently-unresolved canonical eDAR field for this record,
    mapped to its question, in schema order - deterministic (task §10),
    never left to the conversational agent to decide which is "next".

    Covers the 28 flat (non-repeating) fields plus vehicle/casualty
    (Module D/E) slots - but only slots that already have at least one AI
    row (created by csc_apps.recordings.voice_service._bootstrap_entity_fields
    once number_of_vehicles_involved/number_of_persons_involved is KNOWN, or
    by the batch/audio extraction pipeline's own vehicle_count/casualty_count
    - csc_apps.processing.extraction_service._assess). A vehicle/casualty
    that hasn't been established to exist yet is never pre-guessed into
    missingFields - this reads existing rows only, it never decides on its
    own how many slots "should" exist.

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

    for entity in ('vehicle', 'casualty'):
        base_keys = repeating_field_keys(schema, entity)
        indices = sorted({
            int(key.split('.')[1]) for key in unknown_keys if key.startswith(f'{entity}.')
        })
        for index in indices:
            for base_key in base_keys:
                key = f'{entity}.{index}.{base_key}'
                if key in unknown_keys:
                    field_def = resolve_field_key(key, schema=schema)
                    missing[key] = _entity_question_text(field_def, entity, index)

    return missing


def _question_text(field_def: dict) -> str:
    """Plain English prompt text built from the schema's own field name.
    Sarvam/Flutter owns the conversation (task §11) and is free to rephrase
    or localize this before speaking it - Django's job is only to say which
    field is missing, not to produce final conversational copy."""
    return f"What was the {field_def['name'].lower()}?"


def _entity_question_text(field_def: dict, entity: str, index: int) -> str:
    """Same plain-English convention as _question_text, with the entity/index
    made explicit (e.g. "For vehicle 1, what was the vehicle type?") since a
    bare field name alone ("vehicle type") wouldn't say which vehicle."""
    return f"For {entity} {index}, what was the {field_def['name'].lower()}?"
