"""
Django settings for the Crash Scene Co-Pilot (csc) project.

Structured to match pms/settings.py (docs/pms-reference-analysis.md §1) - same
DRF/JWT/drf-spectacular stack, same decouple-based configuration split.
"""

from pathlib import Path
from datetime import timedelta
import os

from decouple import config

from csc.config import Configurations

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='csc-insecure-dev-key-change-in-production')

DEBUG = Configurations.debug

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*').split(',')


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',

    'csc_apps.common',
    'csc_apps.authentication',
    'csc_apps.activity_log',
    'csc_apps.recordings',
    'csc_apps.processing',
    'csc_apps.edar',
]

AUTH_USER_MODEL = 'authentication.User'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

EXTERNAL_MIDDLEWARE = [
    'csc_apps.activity_log.middlewares.log_middleware.LogMiddleware',
]

MIDDLEWARE += EXTERNAL_MIDDLEWARE

ROOT_URLCONF = 'csc.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'csc.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': Configurations.db_name,
        'USER': Configurations.db_user,
        'PASSWORD': Configurations.db_password,
        'HOST': Configurations.db_host,
        'PORT': Configurations.db_port,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static')

# Audio/media is never served as a static public path - docs/security-baseline.md §3.
# MEDIA_ROOT/MEDIA_URL below are Django defaults kept for any future non-evidence
# static-ish asset; actual crash audio never lands here - it goes to
# PRIVATE_STORAGE_ROOT (csc_apps.recordings.storage.LocalPrivateAudioStorage), which
# is never mounted under a URL at all. Production backend selection remains
# docs/open-decisions.md OD-008.
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

PRIVATE_STORAGE_ROOT = (
    Configurations.private_storage_root
    if os.path.isabs(Configurations.private_storage_root)
    else os.path.join(BASE_DIR, Configurations.private_storage_root)
)
AUDIO_MAX_UPLOAD_SIZE_BYTES = Configurations.audio_max_upload_size_bytes

# Must stay above AUDIO_MAX_UPLOAD_SIZE_BYTES: Django raises a bare (non-JSON,
# non-enveloped) RequestDataTooBig error the moment request.data/.FILES is first
# accessed if the request body exceeds this, which would happen *before*
# csc_apps.recordings.validators ever runs and bypass the shared
# {status,message,error} response envelope. Keeping this ceiling above our own
# application-level limit guarantees the friendly, enveloped validation error is
# always the one a client sees for an oversized upload.
DATA_UPLOAD_MAX_MEMORY_SIZE = AUDIO_MAX_UPLOAD_SIZE_BYTES + 1024 * 1024

# Must also stay above AUDIO_MAX_UPLOAD_SIZE_BYTES: below this per-file threshold
# (Django's own default is 2.5MB) an uploaded file is an in-memory
# InMemoryUploadedFile; above it, Django buffers to an on-disk TemporaryUploadedFile
# wrapping a real OS file handle. csc_apps.common.serializer_validations deep-copies
# request.data to get a mutable QueryDict (`data.copy()`), and deepcopy cannot copy
# that file handle (`TypeError: cannot pickle 'BufferedRandom' instances`) - any
# accepted audio file above the default threshold made every upload past ~2.5MB fail
# with an unhandled 500 instead of the shared response envelope. Keeping every
# accepted file in memory sidesteps this entirely.
FILE_UPLOAD_MAX_MEMORY_SIZE = AUDIO_MAX_UPLOAD_SIZE_BYTES + 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'csc_apps.authentication.authentication.JWTAuthentication',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=Configurations.access_token_lifetime_days),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=Configurations.access_token_lifetime_days),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Crash Scene Co-Pilot API',
    'DESCRIPTION': 'Authentication and audio ingestion (Phase 0/1). See docs/api-architecture.md.',
    'VERSION': '0.2.0',
}
