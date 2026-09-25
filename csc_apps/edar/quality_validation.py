"""Deterministic quality validation for an AI eDAR candidate
(docs/phase5-validation-provenance.md). Pure functions over plain dicts - no Django
models, no provider types, no network - so it runs identically at extraction time and
in tests, and never calls an AI provider (no "ask Gemini to fix it" repair loop).

Two outcomes for a candidate:
  - INVALID: at least one structural error (type/enum/confidence/provenance/entity
    limit). The caller rejects the whole candidate; nothing is persisted.
  - VALIDATED / VALIDATION_WARNING: no errors. Warnings flag things a reviewer should
    look at (untraceable evidence, low model confidence, count mismatches) without
    invalidating the candidate.

None of these statuses means "approved" - VALIDATED != APPROVED. And nothing here
computes an accuracy score: there is no ground truth at extraction time.
"""

import dataclasses
import re
import unicodedata

from csc_apps.edar.schema_validation import EdarValidationError, resolve_field_key, validate_extraction_entry

STATUS_VALIDATED = 'VALIDATED'
STATUS_VALIDATION_WARNING = 'VALIDATION_WARNING'
STATUS_INVALID = 'INVALID'

SEVERITY_ERROR = 'error'
SEVERITY_WARNING = 'warning'

# Review-attention signal only - NOT a truth threshold. Gemini's confidence is a
# model-generated, uncalibrated number (docs/phase5-validation-provenance.md
# §Confidence semantics); a value below this is still kept, still `known`, never
# discarded - it just gets a `low_confidence` warning so a reviewer knows where to
# look first. 0.5 is a deliberately round, non-tuned midpoint, not a calibrated cut.
LOW_CONFIDENCE_THRESHOLD = 0.5

EVIDENCE_MATCH_RULE = (
    'case-folded, NFKC-normalized, whitespace-collapsed, edge-punctuation-stripped '
    'substring match against the English transcript; an ellipsis ("..." or U+2026) '
    'splits evidence into fragments that must each match'
)

# Generic, value-free messages per code. Issue messages are stored and returned
# through the API, so they never embed the offending value or any transcript text.
ISSUE_MESSAGES = {
    'invalid_field_key': 'Field is not part of the eDAR schema.',
    'entity_limit_exceeded': 'More repeated entities than the eDAR schema allows.',
    'invalid_type': 'Value does not match the field\'s declared type.',
    'invalid_enum': 'Value is not one of the field\'s allowed values.',
    'invalid_confidence': 'Confidence must be a number between 0 and 1 inclusive.',
    'provenance_incomplete': 'Confidence and evidence must be provided together.',
    'invalid_evidence_timing': 'Evidence start time is after its end time.',
    'invalid_value': 'Value failed validation.',
    'duplicate_field': 'Field was returned more than once.',
    'known_value_inconsistent': 'Known/unknown state disagrees with the stored value.',
    'evidence_not_traceable': 'Evidence could not be located in the English transcript.',
    'missing_evidence': 'Known value has no supporting evidence.',
    'missing_confidence': 'Known value has no model confidence.',
    'low_confidence': 'Model confidence is below the review-attention threshold.',
    'duplicate_label': 'Multi-label value contains duplicate labels.',
    'vehicle_count_mismatch': 'Declared vehicle count is lower than the vehicle records captured.',
    'vehicle_records_incomplete': 'Declared vehicle count exceeds the vehicle records captured.',
    'person_count_mismatch': 'Declared person count is lower than the person records captured.',
}

_EDGE_PUNCT_RE = re.compile(r'^[\W_]+|[\W_]+$', re.UNICODE)
_WHITESPACE_RE = re.compile(r'\s+')
_ELLIPSIS_RE = re.compile(r'\.{3,}|…')

_FREE_TEXT_CATEGORICAL_TYPES = {
    'categorical', 'multi_label_categorical', 'categorical_or_boolean', 'categorical_or_text', 'text_or_categorical',
}


@dataclasses.dataclass
class CandidateAssessment:
    rows: list[dict]
    report: dict


def issue(field: str | None, code: str, severity: str) -> dict:
    return {'field': field, 'code': code, 'severity': severity, 'message': ISSUE_MESSAGES[code]}


def normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    normalized = _WHITESPACE_RE.sub(' ', normalized).strip()
    return _EDGE_PUNCT_RE.sub('', normalized)


def is_evidence_traceable(evidence: str, transcript_text: str) -> bool:
    """True only if every ellipsis-separated fragment of `evidence` appears in the
    English transcript under EVIDENCE_MATCH_RULE. This proves the evidence text
    exists in the persisted transcript - it does not prove the evidence supports the
    extracted value (that judgement stays with the model, then the reviewing officer)."""
    haystack = normalize_for_match(transcript_text)
    fragments = [normalize_for_match(f) for f in _ELLIPSIS_RE.split(evidence)]
    fragments = [f for f in fragments if f]
    return bool(fragments) and all(f in haystack for f in fragments)


def assess_candidate(
    entries: list[dict],
    expected_keys: list[str],
    transcript_text: str,
    schema: dict,
    vehicle_count: int,
    casualty_count: int,
) -> CandidateAssessment:
    """`entries` are the KNOWN fields the provider returned, each
    {"field", "value", "confidence", "source": {"transcript_segment", ...} | None}.
    `expected_keys` is every field key that must end up with a row (KNOWN or UNKNOWN).
    Returns the rows to persist plus the structured report; if `report['status']` is
    INVALID the caller must not persist `rows`."""
    errors: list[dict] = []
    warnings: list[dict] = []

    for entity, count in (('vehicle', vehicle_count), ('casualty', casualty_count)):
        _check_entity_limit(entity, count, schema, errors)

    valid_entries: dict[str, dict] = {}
    for entry in entries:
        key = entry['field']
        if key in valid_entries:
            errors.append(issue(key, 'duplicate_field', SEVERITY_ERROR))
            continue
        try:
            validate_extraction_entry(entry, schema=schema)
        except EdarValidationError as e:
            errors.append(issue(key, e.code, SEVERITY_ERROR))
            continue
        except ValueError:
            errors.append(issue(key, 'invalid_value', SEVERITY_ERROR))
            continue
        valid_entries[key] = entry

    if not errors:
        for key, entry in valid_entries.items():
            warnings.extend(_field_warnings(key, entry, transcript_text))
        warnings.extend(_cross_field_warnings(valid_entries, vehicle_count, casualty_count))

    rows = _build_rows(expected_keys, valid_entries) if not errors else []
    metrics = compute_metrics(rows, schema, warnings, errors)
    status = STATUS_INVALID if errors else (STATUS_VALIDATION_WARNING if warnings else STATUS_VALIDATED)
    report = {
        'status': status,
        'errors': errors,
        'warnings': warnings,
        'metrics': metrics,
        'evidenceMatchRule': EVIDENCE_MATCH_RULE,
    }
    return CandidateAssessment(rows=rows, report=report)


def _check_entity_limit(entity: str, count: int, schema: dict, errors: list[dict]) -> None:
    module = next(m for m in schema['modules'] if m.get('repeat_entity') == entity)
    maximum = module.get('max_repetitions')
    if maximum is not None and count > maximum:
        errors.append(issue(f'{entity}s', 'entity_limit_exceeded', SEVERITY_ERROR))


def _field_warnings(key: str, entry: dict, transcript_text: str) -> list[dict]:
    found = []
    source = entry.get('source')
    evidence = source.get('transcript_segment') if source else None

    if not evidence:
        found.append(issue(key, 'missing_evidence', SEVERITY_WARNING))
    elif not is_evidence_traceable(evidence, transcript_text):
        found.append(issue(key, 'evidence_not_traceable', SEVERITY_WARNING))

    confidence = entry.get('confidence')
    if confidence is None:
        found.append(issue(key, 'missing_confidence', SEVERITY_WARNING))
    elif confidence < LOW_CONFIDENCE_THRESHOLD:
        found.append(issue(key, 'low_confidence', SEVERITY_WARNING))

    value = entry.get('value')
    if isinstance(value, list) and len(set(map(str, value))) != len(value):
        found.append(issue(key, 'duplicate_label', SEVERITY_WARNING))
    return found


def _cross_field_warnings(entries: dict[str, dict], vehicle_count: int, casualty_count: int) -> list[dict]:
    """Only relationships between fields the eDAR spec itself defines (Module C's
    declared counts vs. Module D/E's captured records). Warnings, never errors: a
    declared count above the captured records is legitimate (e.g. a 4th vehicle
    the 3-vehicle cap cannot hold), so it is surfaced for review, not rejected."""
    found = []
    declared_vehicles = entries.get('number_of_vehicles_involved', {}).get('value')
    if isinstance(declared_vehicles, int) and not isinstance(declared_vehicles, bool):
        if declared_vehicles < vehicle_count:
            found.append(issue('number_of_vehicles_involved', 'vehicle_count_mismatch', SEVERITY_WARNING))
        elif declared_vehicles > vehicle_count:
            found.append(issue('number_of_vehicles_involved', 'vehicle_records_incomplete', SEVERITY_WARNING))
    declared_persons = entries.get('number_of_persons_involved', {}).get('value')
    if isinstance(declared_persons, int) and not isinstance(declared_persons, bool):
        if declared_persons < casualty_count:
            found.append(issue('number_of_persons_involved', 'person_count_mismatch', SEVERITY_WARNING))
    return found


def _build_rows(expected_keys: list[str], valid_entries: dict[str, dict]) -> list[dict]:
    rows = []
    for key in expected_keys:
        entry = valid_entries.get(key)
        if entry is not None:
            source = entry.get('source') or {}
            rows.append({
                'field_key': key,
                'known': 'KNOWN',
                'value': entry['value'],
                'confidence': entry.get('confidence'),
                'source_transcript_segment': source.get('transcript_segment'),
                'source_start_time': source.get('start_time'),
                'source_end_time': source.get('end_time'),
            })
        else:
            rows.append({
                'field_key': key, 'known': 'UNKNOWN', 'value': None, 'confidence': None,
                'source_transcript_segment': None, 'source_start_time': None, 'source_end_time': None,
            })
    return rows


def compute_metrics(rows: list[dict], schema: dict, warnings: list, errors: list) -> dict:
    """Deterministic counts only. `fieldCoverageRatio` is COVERAGE (share of attempted
    fields the transcript supported) - explicitly not accuracy or correctness, and
    there is deliberately no aggregate accuracy/confidence score anywhere.

    Public (not assess_candidate-only) since Phase 10B's supplemental-extraction
    merge (csc_apps.processing.extraction_service) also needs to recompute this
    summary for an EdarRecord's full current AI row set after a targeted merge,
    without re-running the whole candidate-validation pipeline (docs/phase10b-
    supplemental-audio.md §Quality summary refresh). `rows` here only needs
    `field_key`/`known`/`confidence`/`source_transcript_segment` per item - the same
    shape assess_candidate's own `_build_rows` produces, or a `.values(...)`
    projection of EdarFieldValue rows."""
    known = [r for r in rows if r['known'] == 'KNOWN']
    warned_untraceable = {w['field'] for w in warnings if w['code'] == 'evidence_not_traceable'}
    with_evidence = [r for r in known if r['source_transcript_segment']]
    free_text = 0
    for row in known:
        field_def = resolve_field_key(row['field_key'], schema=schema)
        if field_def['data_type'] in _FREE_TEXT_CATEGORICAL_TYPES and field_def.get(
            'allowed_values_status'
        ) != 'resolved_from_source':
            free_text += 1
    total = len(rows)
    return {
        'totalFields': total,
        'knownFields': len(known),
        'unknownFields': total - len(known),
        'knownFieldsWithEvidence': len(with_evidence),
        'knownFieldsWithVerifiedEvidence': len([r for r in with_evidence if r['field_key'] not in warned_untraceable]),
        'knownFieldsWithoutEvidence': len(known) - len(with_evidence),
        'knownFieldsWithConfidence': len([r for r in known if r['confidence'] is not None]),
        'lowConfidenceFields': len([w for w in warnings if w['code'] == 'low_confidence']),
        'freeTextCategoricalFields': free_text,
        'fieldCoverageRatio': round(len(known) / total, 4) if total else 0.0,
        'warningCount': len(warnings),
        'errorCount': len(errors),
    }
