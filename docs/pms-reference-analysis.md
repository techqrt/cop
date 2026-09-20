# PMS Reference Analysis

Source inspected: `/Users/rayyanshaikh/VSProjects/PMS` (Django 5.2 property-management backend).
This document records what was actually observed in PMS. Every convention adopted by
Crash Scene Co-Pilot (CSC) traces back to a line item here — nothing below is invented.

## 1. Stack

| Concern | PMS choice | Evidence |
|---|---|---|
| Framework | Django 5.2.7 + Django REST Framework 3.16 | `requirements.txt` |
| Auth tokens | `djangorestframework_simplejwt` is installed but **not used** for verification; PMS hand-rolls JWT issuing/verification with `PyJWT` | `authentication/authentication.py` |
| API docs | `drf-spectacular` (OpenAPI schema, Swagger UI, Redoc) | `pms/urls.py`, every `controller.py` |
| DB | PostgreSQL via `psycopg2-binary`, `django.db.backends.postgresql` | `pms/settings.py` |
| Config | `python-decouple` reading a `.env` file, wrapped in a `Configurations` class | `pms/config.py` |
| CORS | `corsheaders`, currently wide open (`CORS_ALLOW_ALL_ORIGINS = True`) | `pms/settings.py` |
| Background jobs | **None found.** No Celery, RQ, django-q, or `threading`-based task runner anywhere in `pms_apps/` or `pms/`. All processing is synchronous, in-request. | grep across repo, zero hits |
| Object storage | Local disk only, via Django's default `FileField`/`ImageField` + `MEDIA_ROOT`. No S3/GCS abstraction. | `pms/settings.py`, `property/image_utils.py` |
| Frontend | Separate repo (`PMS-Frontend`), not inspected — out of scope for a backend-conventions analysis. | — |

**Implication for CSC:** PMS has no precedent for asynchronous processing or private/cloud
object storage. Phase 0 must establish clean abstractions for both rather than copy a
pattern that doesn't exist (see [architecture-decisions.md](architecture-decisions.md)
ADR-009, ADR-011, and [open-decisions.md](open-decisions.md)).

## 2. Project layout

```
PMS/
  manage.py
  pms/                    # settings package, named after the project
    settings.py, urls.py, wsgi.py, asgi.py
    config.py             # Configurations class wrapping decouple.config()
    constants.py          # Constants class - shared user-facing string constants
    .env                  # local secrets (present in the working tree - see §7)
  pms_apps/                # every domain app lives here, one folder per business domain
    authentication/
    lead/
    property/
    activity_log/
    common/                # shared infrastructure, not a business domain
    helper_apis/           # lookup/reference data (country, city, nationality)
    ...
  media/                   # user uploads, served locally
  requirements.txt
```

Apps are grouped by **business department** (`lead`, `marketing`, `finance`, `legal`, `IT`,
`reception`, ...), each a self-contained Django app under a single `pms_apps/` package
rather than at the repo root. `common` and `helper_apis` are cross-cutting/reference apps,
not domains.

**Adopted for CSC:** one top-level apps package (`csc_apps/`), one app per bounded domain,
`common` as shared infrastructure. Rejected: PMS's department-per-app split, since CSC's
domains are pipeline stages (recordings, processing, eDAR), not organizational departments.

## 3. Per-app internal structure

Observed in `pms_apps/lead/` (the most complete example) and cross-checked against
`authentication/`, `activity_log/`, `property/`:

```
<app>/
  apps.py            # AppConfig, default_auto_field = BigAutoField
  admin.py            # present but usually left as the django-admin stub
  urls.py             # thin: path() -> ControllerClass.method
  controller.py        # HTTP-facing layer: @extend_schema + @api_view + validation decorator
  views.py             # business-logic layer: a "<Domain>View" class, one "<action>_extract" method per operation
  utils.py             # app-local helpers (e.g. field-name mapping for get/get_all)
  tests.py             # Django TestCase + DRF APIClient
  models/<entity>.py   # or flat models.py for single-model apps
  serilizers/request/<action>.py     # [sic] - consistent misspelling across the whole codebase
  serilizers/response/<action>.py
  dataclasses/request/<action>.py    # typed params the view methods actually receive
  migrations/
```

Naming note: the directory is spelled `serilizers` (not `serializers`) everywhere in PMS
except one outlier (`authentication/serializers_auth.py`, a flat file). This is a repo-wide
typo, not a style CSC should propagate — it is called out explicitly here so it's traceable
as a *rejected* pattern, not an oversight.

### Layering / request flow

```
URL (urls.py)
  -> controller.py       @extend_schema (OpenAPI) + @api_view([...]) + @SerializerValidations(...).validate
       -> validates auth (request.user.is_authenticated), validates body+query via a
          request Serializer, turns validated_data into a typed dataclass, attaches it
          as request.params (with user_id and, for GET, present_url auto-injected)
       -> views.py         "<Domain>View().<action>_extract(params=request.params)"
            @Common().exception_handler   - central try/except -> uniform error Response
            [@Common().<domain>_validation - optional extra cross-field validation]
            wraps DB work in transaction.atomic()
            calls static/instance methods on models/<entity>.py for all persistence
            returns rest_framework.response.Response via Utils.success_response_data(...)
       -> models/<entity>.py
            Django Model with domain static methods: create(), update(), remove(), get(),
            get_all() - "fat model" / active-record style. update() uses a NOT_PROVIDED
            sentinel to distinguish "field omitted from request" from "field explicitly
            cleared to null".
```

**Adopted for CSC:** the same controller -> view -> model layering, the same decorator-based
cross-cutting concerns (`Common().exception_handler`, serializer-driven request validation),
the same `NOT_PROVIDED` sentinel for partial updates, the same request/response dataclass +
serializer split. Renamed `serilizers/` -> `serializers/` (fixing the typo; not a functional
deviation, just correct spelling for a fresh codebase — recorded as ADR-013).

## 4. Models

- Every model declares `class Meta: db_table = "<snake_case_name>"` explicitly rather than
  relying on Django's default `<app>_<model>` table name.
- `choices` are plain lists of `(value, value)` tuples defined as class attributes
  (`LEAD_TYPES`, `PURPOSE_CHOICES`, ...), not `TextChoices`/`IntegerChoices` enums.
- `created_at`/`updated_at` via `auto_now_add=True` / `auto_now=True` is the standard
  audit-timestamp pair.
- Foreign keys mostly use `on_delete=models.DO_NOTHING`, leaving referential cleanup to the
  application layer rather than cascading deletes.
- No soft-delete convention was found — `remove()` methods call `.delete()` directly. `Lead`
  and other domain records instead carry an `is_active` boolean used as a status flag.
- `DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'` project-wide.

**Adopted for CSC:** explicit `db_table`, plain tuple `choices`, explicit `created_at`/
`updated_at`. **Deviation:** CSC uses `on_delete=models.PROTECT` for evidence-bearing
relationships (Recording -> Audio, EdarRecord -> Recording) because raw audio and
AI-extracted data must never silently disappear via a cascading delete — this is a direct
consequence of the "raw evidence is immutable" requirement, not a PMS pattern (documented
as ADR-002 deviation, not a silent one).

## 5. Serializers & dataclasses

- **Request serializers** (`serilizers/request/*.py`) are `rest_framework.serializers.Serializer`
  subclasses (not `ModelSerializer`) and accept the raw incoming field names. They implement
  `.create(validated_data)` to return a typed `dataclasses.dataclass` (in `dataclasses/request/`)
  rather than returning a dict or a model instance. This dataclass is what `views.py` methods
  actually receive as `params`.
- **Response serializers** (`serilizers/response/*.py`) are also plain `Serializer` subclasses,
  and their field names are **camelCase** (`leadId`, `firstName`, `createdAt`) even though the
  underlying DB columns and request fields are snake_case. This is the one deliberate
  naming-convention split in the codebase: snake_case in, camelCase out.
- A generic `GetAllSerializer` / `GetAll` dataclass (`common/dataclasses/get_all.py`) standardizes
  list-endpoint query params: `values`, `page_num`, `limit` (capped at
  `Configurations.max_pagination_limit`), `sort_by`, `sort_order`, `filter_key`, `filter_value`,
  `search_key`, `from_date`, `to_date`.

**Adopted for CSC:** plain `Serializer` (not `ModelSerializer`) + typed dataclass params,
snake_case-in/camelCase-out, and the shared `GetAll` pagination dataclass reused verbatim
for every list endpoint.

## 6. Cross-cutting infrastructure (`pms_apps/common/`)

| File | Purpose |
|---|---|
| `common.py` | `Common` class: `exception_handler` decorator (uniform try/except -> `Response`), optional extra validation decorators (e.g. `country_city_validation`), and an optional `response_handler` serializer that validates the *outgoing* payload before it's returned. |
| `utils.py` | `Utils`: `success_response_data(message, data)` -> `{status: True, message, data}`; `error_response_data(message, error)` -> `{status: False, message, error}`; `add_page_parameter(...)` for paginated envelopes; `env_exception_handler` hides raw exception text in production (`Configurations.debug` gate). |
| `serializer_validations.py` | `SerializerValidations(serializer).validate` decorator: enforces `request.user.is_authenticated`, merges query params into body data, runs the serializer, and attaches the resulting dataclass to `request.params`. |
| `sentinels.py` | `NOT_PROVIDED` singleton — falsy, distinct from `None`, used as a default for "not sent in this request" on every partial-update method. |
| `exceptions/validation_errors.py`, `exceptions/token_errors.py` | Custom exception types with an `.errors: list` attribute, caught specifically (before the generic `Exception` branch) by `Common.exception_handler`. |
| `dataclasses/get_all.py`, `dataclasses/get.py`, `dataclasses/search.py`, `dataclasses/download.py` | Shared, reusable request-shape dataclasses. |
| `models/permissions.py` | Small permission-flag models referenced by domain models. |

**Adopted for CSC almost verbatim** — this is genuinely reusable infrastructure, not PMS
business logic, so it is the one area copied closely rather than just "inspired by".
Response envelope (`{status, message, data}` / `{status, message, error}`) is reused
unchanged so any future shared frontend tooling keeps working across both systems.

## 7. Authentication & authorization

- `pms_apps/authentication/models.py`: a custom `User(AbstractBaseUser)` — **not**
  Django's default `auth.User`, though `AUTH_USER_MODEL` is still (inconsistently) set to
  `'auth.User'` in settings while the app clearly intends its own model. Fields: `user_id`
  (AutoField PK), `phone_number` (unique), `name`, `email`, `department`, `role`,
  `access_token`/`refresh_token` (raw JWT strings stored on the row), `otp`/`otp_expiry`.
- `authentication/authentication.py`: `JWTAuthentication(BaseAuthentication)` — manually
  decodes a Bearer JWT with `PyJWT` using `settings.SECRET_KEY` (HS256), then **compares the
  presented token against `User.access_token` stored in the DB**. This is a deliberate
  single-active-session mechanism: issuing a new token elsewhere invalidates the old one
  immediately, because the stored value changes. `rest_framework_simplejwt` is installed but
  its blacklist/rotation machinery is unused.
- Authorization is **department-string-based**: the JWT payload carries `department` and
  `role`; `User.get_permissions()` dispatches to a per-department permission lookup, and
  `JWTAuthentication.validate_permissions()` checks the requested URL path against a
  `department -> URL-segment` table, denying access to any department segment the caller
  doesn't hold a permission flag for.
- `pms/constants.py` centralizes user-facing auth error strings (`auth_error`,
  `access_token_expired`, `forbidden_access`, ...).

**Adopted for CSC:** the token-issuance/verification mechanism (DB-stored token compared
against the Bearer token -> single active session), the `BaseAuthentication` subclass
pattern, and centralizing auth error copy in `constants.py`. **Not adopted:** the
department-string permission model — CSC's role model is flat (`Officer`, `Reviewer`,
`Admin`), not multi-department, so `role`-only checks replace PMS's `department` + `role`
matrix (documented in `docs/security-baseline.md`).

## 8. Error handling

All exceptions funnel through `Common.exception_handler`, in this order: `ValueError` ->
400, `FileExistsError` -> 400, `ValidationErrors` (custom) -> 400 with a structured error
list, `TokenErrors` (custom) -> 401, `jwt.exceptions.InvalidSignatureError` -> 401,
generic `Exception` -> 400 with message redacted outside debug mode (and a special-cased
foreign-key-constraint message). There is no distinction anywhere in PMS between retryable
and non-retryable failures — every error is terminal and returned synchronously to the
caller, which is consistent with PMS having no background processing to retry in the first
place.

**Adopted for CSC:** the funnel-through-one-decorator pattern and the custom-exception-
with-`.errors`-list shape. **New for CSC (no PMS precedent):** a retryable/non-retryable
classification, because CSC's AI pipeline runs asynchronously and must be able to retry
transient provider failures — see `docs/error-retry-strategy.md`.

## 9. Observability / audit trail

`pms_apps/activity_log/`: a `LogMiddleware` stashes `ip_address`, `user_agent`, `end_point`,
`method` into a `threading.local()` per request; a Django **signal** (`signals/log_signal.py`)
writes an `ActivityLog` row (`user`, `action` [Create/Update/Delete], `model`, `method`,
`end_point`, `details` JSON, `created_on`) whenever a tracked model is saved. Older rows are
moved to a separate `ArchiveLog` table and both are queried via `.union()` for history views.

**Adopted for CSC:** the middleware-plus-thread-local-context pattern, and a JSON `details`
column for flexible per-event metadata. **Extended for CSC:** PMS's activity log only
records CRUD actions; CSC additionally needs a *processing-lifecycle* event log (STT
started/completed, translation started/completed, ...) — modeled as `ProcessingEvent` in the
`processing` app rather than overloading `ActivityLog`, since the two have different
consumers (a security/compliance audit trail vs. a per-recording pipeline timeline). See
`docs/observability.md`.

## 10. Testing

`django.test.TestCase` + `rest_framework.test.APIClient`, `force_authenticate(user=...)` to
skip real login in tests, fixtures built by direct `Model.objects.create(...)` calls in
`setUp` (no factory library — `factory_boy` is not a dependency), one `TestCase` subclass per
behavior with a docstring stating the behavior under test, assertions on
`response.status_code` and `response.data["data"]`.

**Adopted for CSC** without modification — see `csc_apps/*/tests.py` for Phase 0 examples.

## 11. Dependency policy in practice

`requirements.txt` is a flat, pinned list with no dev/test/prod split and no extras. Every
dependency is a widely-used, actively maintained library; nothing exotic. No linter/formatter
config (no `.flake8`, `pyproject.toml`, `ruff.toml`, or `black` config) was found anywhere in
the repo — code style is enforced only by convention and `README copy.md`'s five naming/
style rules (see below), not by tooling.

**Adopted for CSC:** same flat, pinned `requirements.txt` style; no new formatter/linter
introduced in Phase 0 (would be a process change PMS itself hasn't made — flagged as an open
decision, not silently added).

## 12. Documented style rules (`README copy.md`)

PMS's own contribution guide states, verbatim:
- snake_case variables, ALL_CAPS constants, PascalCase classes.
- Single-responsibility, short self-explanatory function names, avoid nesting.
- "Avoid `try..except`, instead add more validations" — i.e. prefer raising a specific,
  caught exception type (`ValueError`, `ValidationErrors`) over broad exception handling in
  business logic; the broad `except Exception` belongs only in the one shared
  `Common.exception_handler`.
- Type-annotate argument and return types on functions (observed consistently in `models/
  *.py` and `views.py` across the codebase, even though nothing enforces it).

**Adopted for CSC verbatim** — these five rules govern every file written in Phase 0.

## 13. What was deliberately NOT copied

Per the "same engineering standards, different domain" principle:
- No PMS domain models (`Lead`, `Property`, `PropertyAssignment`, ...) or department apps
  (`marketing`, `finance`, `legal`, `IT`, `HR`, `owner`, `reception`, `collection`,
  `checkin_checkout`, `general_manager`, `supervisor`, `store_manager`, `maintenance`) were
  copied or referenced by CSC code.
- `helper_apis` (country/city/nationality lookups) was not copied; CSC has no equivalent
  lookup-table need in Phase 0.
- The `department`-based permission matrix was not copied (see §7).
- No PMS secrets, `.env` values, or media files were copied.
