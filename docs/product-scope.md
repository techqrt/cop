# Product Scope — Crash Scene Co-Pilot (CSC)

## 1. What this is

CSC is a field tool for police/traffic officers to capture a crash scene report by
**speaking**, instead of manually filling a paper or digital eDAR form. The officer records
(or uploads) audio at the scene; the system transcribes it, translates it to English,
extracts a fixed set of eDAR fields, and hands the officer a pre-filled record to review and
approve.

**Source of truth for scope:** this document restates only what is stated or directly
implied by the project brief and `eDAR Fields.pdf`. Where the source is silent, that is
called out as an open decision (`docs/open-decisions.md`), not filled in with an assumption.

## 2. In scope for the product (all phases)

| Capability | Source requirement |
|---|---|
| Live audio recording from the client, with near-real-time (few-second-latency) transcription | SOURCE REQUIREMENT |
| Audio file upload as an alternative input path | SOURCE REQUIREMENT |
| Speech-to-text in the original spoken language | SOURCE REQUIREMENT |
| Mandatory translation of the transcript to English | SOURCE REQUIREMENT |
| Extraction of the 42 eDAR fields (7 modules) from the English transcript | SOURCE REQUIREMENT, `eDAR Fields.pdf` |
| Officer review and approval of the AI-generated record | SOURCE REQUIREMENT |
| Recording/processing history | SOURCE REQUIREMENT |
| Export of the completed eDAR record | SOURCE REQUIREMENT ("eventually" — later capability, not Phase 0) |

## 3. Explicitly out of scope

| Excluded | Why |
|---|---|
| Multi-speaker diarization | SOURCE REQUIREMENT: "A recording contains ONE speaker... Multi-speaker diarization is NOT required for the initial implementation." |
| Any eDAR field requiring lab results, post-mortem data, or court records | SOURCE REQUIREMENT: the 42-field set is explicitly scoped to "what an officer can observe and report within 10 minutes at the scene." |
| Ultra-low-latency (sub-second) live transcription | SOURCE REQUIREMENT: "a delay of a few seconds is acceptable." |
| A general-purpose transcription product | SOURCE REQUIREMENT: "The system is NOT a general-purpose transcription application." |

## 4. Phase 0 scope (this delivery)

Phase 0 delivers the **foundation**, not the working product. Concretely:

- Architecture, domain-model, and process documentation (this `docs/` tree).
- A machine-readable, versioned eDAR schema (`schemas/edar-schema.json`) covering exactly
  the 42 fields / 7 modules from `eDAR Fields.pdf`.
- A Django project skeleton, structured per `docs/pms-reference-analysis.md`, with:
  - working authentication (email + password + role, JWT, PMS-pattern token invalidation);
  - domain models for Recording, Audio, ProcessingJob/ProcessingEvent, and the eDAR
    field-value store (see `docs/domain-model.md`) — schema and migrations only, **no**
    CRUD API surface yet;
  - provider-agnostic interfaces (not implementations) for speech-to-text, translation, and
    extraction;
  - an async-processing abstraction (interface + a dev-only inline runner), with the actual
    queue technology left as an open decision.
- Representative tests demonstrating the testing pattern (not full coverage).

Phase 0 explicitly does **not** implement: live audio capture UI, the working STT/translation
/extraction pipeline against a real provider, the review/approval UI or API, export, or
recording-history endpoints. These are Phase 1+.

## 5. Primary users

- **Field Officer** — records/uploads audio, reviews the AI-generated record, approves or
  edits it. (ROLE — exact role list is an ARCHITECTURAL DECISION in
  `docs/security-baseline.md`, not enumerated in the source brief.)
- **Reviewer/Supervisor** — ASSUMPTION, not sourced from the brief; included because "Human
  review" and "officer approval" are named as distinct steps in the processing pipeline and
  a single-role system cannot represent a review handoff. Flagged in
  `docs/open-decisions.md` (OD-001) rather than treated as settled.

## 6. Product principle

> The extracted information is based on a predefined eDAR schema. The AI must never
> fabricate an eDAR value simply because the field exists.

This governs every downstream design choice: schema-first extraction, an explicit
unknown/not-provided data policy (`docs/unknown-data-policy.md`), and mandatory provenance
on every AI-produced value (`docs/data-provenance.md`).
