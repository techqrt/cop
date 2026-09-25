"""The eDAR extraction prompts (docs/phase4-gemini-edar-extraction.md §Extraction
prompt, source instructions §49-50). Kept as dedicated, versioned templates - not
built inline inside a view or service - so they can be reasoned about, tested, and
compared across versions independently of the code that calls Gemini.

One recording's extraction is now three separate Gemini calls - flat crash-level
fields, vehicles, casualties - instead of one combined call (docs/phase4-gemini-edar-
extraction.md §Structured-output strategy). This module has one prompt builder per
call, sharing the same role/rules text: Gemini's response_json_schema is checked
against a protobuf Schema with a hard complexity ceiling, and the single schema that
used to describe everything at once is rejected outright once vehicles and casualties
are added alongside the 28 flat fields (verified empirically 2026-09), independent of
any individual field or keyword - splitting the request, not shrinking what is asked
per field, is what keeps every field's confidence+evidence provenance uniform.
"""

from csc_apps.edar.schema_validation import resolve_field_key
from csc_apps.processing.providers.gemini.schema_adapter import GPS_FIELD_KEY, MAX_CASUALTIES

# Bump this whenever the prompt's instructions change in a way that could affect
# extraction behavior - recorded in EdarFieldValue.extraction_version alongside the
# model and eDAR schema version (docs/phase4-gemini-edar-extraction.md §Provenance,
# source instructions §50) so a later prompt change never retroactively looks like it
# produced older extractions. Bumped from v1 to v2 for the three-call split - the
# instructions each call receives changed, even though the underlying rules did not.
PROMPT_VERSION = 'v2'

# Phase 10B's supplemental/targeted extraction (docs/phase10b-supplemental-
# audio.md) is a distinct prompt shape (a backend-selected field subset plus
# entity-matching context, not the fixed flat/vehicles/casualties split) - versioned
# independently so EdarFieldValue.extraction_version always shows whether a given
# field came from the normal or the supplemental extraction path.
TARGETED_PROMPT_VERSION = 'targeted-v1'

_ROLE_AND_TASK = """\
You are a crash-scene information extraction system used by a police records \
platform. You are given the English-language transcript of a traffic-police \
officer's spoken statement at a crash scene, and a fixed set of eDAR (electronic \
Detailed Accident Report) fields. Your task is to extract, from that transcript \
only, whatever information it actually supports for each of those fields."""

_SOURCE_AND_NO_INVENTION_RULES = """\
RULES (follow all of these exactly):

1. The transcript below is your ONLY source of fact. Do not use general world \
knowledge, typical-crash assumptions, or statistics to fill in a field.
2. Do not invent facts. Do not infer a detail the officer did not state, even if it \
seems statistically likely (e.g. do not assume daylight, clear weather, dry roads, \
or a specific speed limit unless the officer actually says so).
3. If a field is not supported by the transcript, its value MUST be null. A null \
value is correct and expected for most fields most of the time - do not treat \
"leaving many fields null" as a failure.
4. Preserve uncertainty. If the officer expresses doubt ("I believe...", "around...", \
"I'm not sure whether...", "I couldn't determine..."), do not convert that into a \
confident value. For a field where the schema allows an approximate representation \
(e.g. age), you may record the approximate value as stated (e.g. "around 30"). For a \
plain boolean field where the officer's statement is genuinely uncertain rather than \
a stated fact, leave the value null rather than guessing true or false.
5. Never confuse "false" with "unknown". Only set a boolean field to false if the \
transcript affirmatively states the negative (e.g. "he was not wearing a helmet"). \
If the transcript simply never mentions the topic, or the officer says they \
couldn't determine it, the value is null - not false.
6. Preserve numbers and identifiers exactly as stated (vehicle registration numbers, \
FIR/case numbers, ages, speed limits). Do not round, reformat, or "correct" them.
7. Extract only the vehicles and persons the transcript actually provides evidence \
for. Do not invent a vehicle or a person to fill out the data. Never describe more \
than 3 vehicles - if the transcript describes more than 3, extract the 3 the \
transcript gives the clearest, most complete information about, and leave the \
others out entirely (do not fabricate a partial 4th entry).
8. For date fields, extract the crash date only if the transcript states or clearly \
implies it (e.g. "today"). Never substitute the current date, an upload date, or any \
other system timestamp - if it isn't in the transcript, leave it null.
9. For every field where you provide a non-null value, also provide: a confidence \
score from 0.0 to 1.0 reflecting how directly the transcript supports that exact \
value (not a generic high number), and the short verbatim (or near-verbatim) excerpt \
of the transcript that supports it. If value is null, confidence and evidence must \
also be null.
10. Do not decide GPS coordinates or police-station jurisdiction from map knowledge - \
only report a jurisdiction if the officer actually names one aloud."""

_FLAT_STRUCTURED_OUTPUT_INSTRUCTION = """\
Respond with structured JSON matching exactly the schema provided to you via this \
request's response schema. Do not add commentary, markdown formatting, or any text \
outside the JSON object. Do not add fields that are not in the schema."""

_VEHICLES_STRUCTURED_OUTPUT_INSTRUCTION = """\
Respond with structured JSON matching exactly the schema provided to you via this \
request's response schema: a single object with one key, "vehicles", an array of \
vehicle objects, one per vehicle involved. Do not add commentary, markdown \
formatting, or any text outside the JSON object. Do not add fields that are not in \
the schema. Never include more than 3 vehicles - if the transcript describes more \
than 3, include only the 3 the transcript gives the clearest, most complete \
information about; do not fabricate a partial 4th entry."""

_CASUALTIES_STRUCTURED_OUTPUT_INSTRUCTION = """\
Respond with structured JSON matching exactly the schema provided to you via this \
request's response schema: a single object with one key, "casualties", an array of \
casualty objects, one per person involved (driver, passenger, pedestrian, etc). Do \
not add commentary, markdown formatting, or any text outside the JSON object. Do not \
add fields that are not in the schema. Never include more than {max_casualties} \
casualties."""

_TARGETED_STRUCTURED_OUTPUT_INSTRUCTION = """\
This is a SUPPLEMENTAL follow-up statement, recorded after an earlier statement \
about the same crash. The earlier statement already produced values for most eDAR \
fields; the FIELD REFERENCE below lists only the specific fields that earlier \
statement left unresolved. Extract a value for one of these fields ONLY if this new \
transcript actually supports it - most of them may still end up null, and that is \
expected, not a failure. If this transcript does not mention a field at all, or \
only repeats something already recorded, leave it null rather than restating a \
guess. Respond with structured JSON matching exactly the schema provided to you via \
this request's response schema. Do not add commentary, markdown formatting, or any \
text outside the JSON object. Do not add fields that are not in the schema."""


def _field_reference_line(field_def: dict, display_key: str | None = None) -> str:
    # display_key lets a targeted/supplemental reference (Phase 10B) show the full
    # dotted key ("vehicle.1.registration_number") instead of field_def's own bare
    # base key ("registration_number") - the flat/vehicles/casualties references
    # never pass this, so their output is unchanged.
    key = display_key or field_def['field_key']
    parts = [f"- {key} ({field_def['name']}): type={field_def['data_type']}"]
    if field_def.get('allowed_values_status') == 'resolved_from_source' and field_def.get('allowed_values'):
        parts.append(f"allowed values: {', '.join(field_def['allowed_values'])}")
    elif field_def['data_type'] in ('categorical', 'multi_label_categorical', 'ordinal'):
        parts.append('no fixed value list defined yet - use a short, natural phrase describing the category')
    # `example` (the required output format/shape, e.g. crash_date's "2026-05-14")
    # and `source_note` (how the ORIGINAL-language transcript might phrase this
    # colloquially, e.g. crash_date's "e.g. 'aaj, 14 May'") answer different
    # questions and must both reach Gemini when both are present - previously an
    # `elif` let a field with both silently drop its `example`, which is how
    # crash_date/crash_time (both have a note) reached Gemini with no format
    # guidance at all and came back as "March 12, 2026" / "approximately 6:45 PM"
    # instead of the required "2026-03-12" / "18:45" (live-verified 2026-09,
    # confirmed by re-running the real extraction path end to end).
    example = field_def.get('example')
    if example is not None:
        parts.append(f'example: {example!r}')
    note = field_def.get('source_note') or field_def.get('notes')
    if note:
        parts.append(f'note: {note}')
    return ' | '.join(parts)


def _module_reference(module: dict, exclude: tuple[str, ...] = ()) -> str:
    lines = [f"Module {module['module_id']} - {module['name']}:"]
    for field_def in module['fields']:
        if field_def['field_key'] in exclude:
            continue
        lines.append(_field_reference_line(field_def))
    return '\n'.join(lines)


def build_flat_field_reference(edar_schema: dict) -> str:
    """Human-readable field-by-field reference for the flat crash-level call (Module
    A-minus-GPS/B/C/F/G) - source instructions §16: "the prompt must contain...
    field name, description, expected type, allowed values where applicable,
    extraction instructions" - not just a bare field-name list."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    sections = [
        _module_reference(modules_by_id['A'], exclude=(GPS_FIELD_KEY,)),
        _module_reference(modules_by_id['B']),
        _module_reference(modules_by_id['C']),
        _module_reference(modules_by_id['F']),
        _module_reference(modules_by_id['G']),
    ]
    return '\n\n'.join(sections)


def build_vehicle_field_reference(edar_schema: dict) -> str:
    """Field reference for the vehicles-only call (Module D)."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    return (
        _module_reference(modules_by_id['D'])
        + '\n(one such object per vehicle, in a top-level "vehicles" array, max 3)'
    )


def build_targeted_field_reference(edar_schema: dict, field_keys: list[str]) -> str:
    """Field reference for a Phase 10B supplemental/targeted call (docs/phase10b-
    supplemental-audio.md §Targeted extraction) - exactly the backend-derived
    `field_keys`, dotted keys shown in full so a repeating field's index is visible
    to the model (e.g. "vehicle.1.registration_number"), not just its base name."""
    return '\n'.join(
        _field_reference_line(resolve_field_key(key, schema=edar_schema), display_key=key) for key in field_keys
    )


def build_entity_context_block(context_lines: list[str]) -> str:
    """Optional "already known" summary of the vehicles/casualties a targeted call's
    eligible fields belong to (docs/phase10b-supplemental-audio.md §Entity
    matching) - built by the caller (csc_apps.processing.extraction_service) from
    already-KNOWN AI field values, not by this module (prompt.py stays free of any
    database access). Lets Gemini correctly attribute a new statement like "the
    car's registration was..." to the right existing vehicle index instead of
    guessing; the instruction text makes clear this is for matching only, not
    something to re-report."""
    if not context_lines:
        return ''
    return 'CONTEXT (already recorded from an earlier statement - for matching only, do not re-report):\n' + '\n'.join(
        context_lines
    )


def build_targeted_extraction_prompt(
    english_text: str, edar_schema: dict, field_keys: list[str], entity_context_lines: list[str] | None = None,
) -> str:
    """The Phase 10B supplemental-audio call - one call, restricted to exactly
    `field_keys` (docs/phase10b-supplemental-audio.md §Targeted extraction)."""
    sections = [
        _ROLE_AND_TASK,
        _SOURCE_AND_NO_INVENTION_RULES,
        'FIELD REFERENCE:\n\n' + build_targeted_field_reference(edar_schema, field_keys),
    ]
    context_block = build_entity_context_block(entity_context_lines or [])
    if context_block:
        sections.append(context_block)
    sections.append(_TARGETED_STRUCTURED_OUTPUT_INSTRUCTION)
    sections.append(
        'TRANSCRIPT (the officer\'s supplemental statement, already translated to English):\n"""\n'
        + english_text + '\n"""'
    )
    return '\n\n'.join(sections)


def build_casualty_field_reference(edar_schema: dict) -> str:
    """Field reference for the casualties-only call (Module E)."""
    modules_by_id = {m['module_id']: m for m in edar_schema['modules']}
    return (
        _module_reference(modules_by_id['E'])
        + '\n(one such object per person, in a top-level "casualties" array)'
    )


def _build_prompt(english_text: str, field_reference: str, structured_output_instruction: str) -> str:
    return '\n\n'.join([
        _ROLE_AND_TASK,
        _SOURCE_AND_NO_INVENTION_RULES,
        'FIELD REFERENCE:\n\n' + field_reference,
        structured_output_instruction,
        'TRANSCRIPT (the officer\'s statement, already translated to English):\n"""\n'
        + english_text + '\n"""',
    ])


def build_flat_extraction_prompt(english_text: str, edar_schema: dict) -> str:
    """The first of three calls - flat crash-level fields. `english_text` is
    included verbatim - never summarized, rewritten, or truncated before this point
    (docs/phase4-gemini-edar-extraction.md §9, §75)."""
    return _build_prompt(english_text, build_flat_field_reference(edar_schema), _FLAT_STRUCTURED_OUTPUT_INSTRUCTION)


def build_vehicles_extraction_prompt(english_text: str, edar_schema: dict) -> str:
    """The second of three calls - just the vehicles involved."""
    return _build_prompt(
        english_text, build_vehicle_field_reference(edar_schema), _VEHICLES_STRUCTURED_OUTPUT_INSTRUCTION
    )


def build_casualties_extraction_prompt(english_text: str, edar_schema: dict) -> str:
    """The third of three calls - just the people involved."""
    return _build_prompt(
        english_text, build_casualty_field_reference(edar_schema),
        _CASUALTIES_STRUCTURED_OUTPUT_INSTRUCTION.format(max_casualties=MAX_CASUALTIES),
    )
