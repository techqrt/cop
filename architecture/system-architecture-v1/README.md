# System Architecture — v1 (Phase 0)

This is the Phase 0 architecture snapshot. It will be superseded by v2 once Phase 1 picks
concrete providers and a task-queue technology (`docs/open-decisions.md`).

## 1. Component diagram

```mermaid
flowchart TB
    subgraph Client["Client (mobile/web - not built in Phase 0)"]
        LiveRec["Live recording"]
        Upload["Audio upload"]
    end

    subgraph API["Django API (csc)"]
        Auth["authentication\n(JWT, email+password)"]
        Rec["recordings\n(Recording, Audio, Transcript)"]
        Proc["processing\n(ProcessingJob, ProcessingEvent,\nTaskRunner, provider interfaces)"]
        Edar["edar\n(EdarRecord, EdarFieldValue)"]
        ActLog["activity_log\n(CRUD audit trail)"]
        Common["common\n(shared infra: Utils, Common,\nsentinels, exceptions)"]
    end

    subgraph Storage["Storage"]
        DB[("PostgreSQL")]
        Blob[("Audio storage\n(backend TBD - OD-008)")]
    end

    subgraph Async["Async pipeline (interface only in Phase 0)"]
        Runner["TaskRunner\n(InlineTaskRunner - dev only)"]
        STT["SpeechToTextProvider\n(interface, no impl)"]
        Trans["TranslationProvider\n(interface, no impl)"]
        Ext["ExtractionProvider\n(interface, no impl)"]
    end

    LiveRec -->|"stream/batch audio"| Rec
    Upload -->|"upload audio"| Rec
    Client -->|"Bearer token"| Auth

    Rec --> DB
    Rec --> Blob
    Rec -->|"enqueue job"| Proc
    Proc --> Runner
    Runner --> STT --> Trans --> Ext
    Ext -->|"schema-validated result"| Edar
    Edar --> DB
    Proc --> DB

    Auth --> DB
    ActLog --> DB
    API --> Common
```

## 2. Layering (within the API, per domain app)

```
URL (urls.py)
  -> Controller (controller.py): @extend_schema, @api_view, request validation decorator
    -> View (views.py): "<Domain>View" business logic, @Common().exception_handler,
                          transaction.atomic()
      -> Model (models/*.py): Django ORM, fat-model static methods (create/update/get/get_all)
```

Adopted unchanged from PMS (`docs/pms-reference-analysis.md` §3).

## 3. The three layers of truth, spatially

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 — Raw evidence                                           │
│   Audio (immutable once stored)                                  │
└─────────────────────────────────────────────────────────────────┘
                              │ read-only input to
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2 — AI interpretation                                      │
│   Transcript (ORIGINAL, ENGLISH)                                 │
│   EdarFieldValue (layer=AI) — confidence, source span, version   │
└─────────────────────────────────────────────────────────────────┘
                              │ officer reviews, never overwrites
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3 — Human-approved record                                  │
│   EdarFieldValue (layer=APPROVED) — updated_by, updated_at       │
│   EdarRecord.review_status = APPROVED                            │
└─────────────────────────────────────────────────────────────────┘
```

## 4. What exists in Phase 0 vs. what's drawn for Phase 1

Solid in Phase 0: `authentication`, `common`, `activity_log` (working code), the
`recordings`/`processing`/`edar` **models** and the `TaskRunner`/provider **interfaces**
(no concrete provider, no concrete queue). Everything under "Client" and the concrete
contents of "Async pipeline" boxes other than the interfaces themselves are Phase 1+.

## 5. Related documents

`docs/architecture-decisions.md` (why), `docs/domain-model.md` (exact schema),
`docs/processing-pipeline.md` (stage contracts), `docs/api-architecture.md` (HTTP surface).
