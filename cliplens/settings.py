from pathlib import Path
import os
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured
from urllib.parse import urlparse

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'freestream-dev-secret-key-2024')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

def _origin_from_url(url: str) -> str | None:
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


# ── Site URL — used in templates and API responses for absolute URLs ──────────
# Change SITE_URL in .env — do NOT hardcode here.
SITE_URL = os.getenv('SITE_URL', 'http://localhost:8000').rstrip('/')

# Django 4.0+ CSRF validates the Origin header against this list.
# Chrome always sends Origin on form POSTs; Safari may omit Origin on same-site POSTs.
_site_origin = _origin_from_url(SITE_URL)
_default_csrf_trusted = [
    o for o in [
        _site_origin,
        'http://localhost:8000',
        'http://127.0.0.1:8000',
        'http://0.0.0.0:8000',
    ] if o
]
CSRF_TRUSTED_ORIGINS = [
    o.strip().rstrip('/')
    for o in os.getenv('CSRF_TRUSTED_ORIGINS', ','.join(_default_csrf_trusted)).split(',')
    if o.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
    'rest_framework',
    'corsheaders',
    'django_celery_results',
    'videos',
    'debug_toolbar',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'debug_toolbar.middleware.DebugToolbarMiddleware',
    'videos.middleware.HLSHeadersMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # serve static files in production
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Allow the watch & embed pages to be iframed from anywhere.
# XFrameOptionsMiddleware respects this setting globally;
# individual views that need full exemption use @xframe_options_exempt.
X_FRAME_OPTIONS = 'SAMEORIGIN'

# Comma-separated list of origins allowed to embed your player in an iframe.
# Used in the Content-Security-Policy header on embed responses.
# Set to '*' to allow all, or list specific origins e.g. 'https://lms.essar.com'
EMBED_ALLOW_ORIGINS = os.getenv('EMBED_ALLOW_ORIGINS', '*')

ROOT_URLCONF = 'cliplens.urls'

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
                'videos.context_processors.site_url',        # injects SITE_URL into all templates
                'videos.context_processors.user_role',       # injects is_editor / is_superadmin / user_role
                'videos.context_processors.sidebar_context', # injects subscribed_channels for sidebar
            ],
        },
    },
]

WSGI_APPLICATION = 'cliplens.wsgi.application'

_use_mssql    = os.getenv('USE_MSSQL',    'false').lower() == 'true'
_use_mysql    = os.getenv('USE_MYSQL',    'false').lower() == 'true'
_use_postgres = os.getenv('USE_POSTGRES', 'true').lower()  == 'true'

_active = sum([_use_mssql, _use_mysql, _use_postgres])
if _active > 1:
    raise ImproperlyConfigured(
        "Exactly one of USE_MSSQL, USE_MYSQL, USE_POSTGRES must be true in .env"
    )
if _active == 0:
    raise ImproperlyConfigured(
        "No database backend selected — set USE_POSTGRES=true (or USE_MYSQL / USE_MSSQL) in .env"
    )

if _use_mssql:
    DATABASES = {
        'default': {
            'ENGINE': 'mssql',
            'NAME': os.getenv('MSSQL_DB_NAME', 'ClipLens'),
            'USER': os.getenv('MSSQL_DB_USER', ''),
            'PASSWORD': os.getenv('MSSQL_DB_PASSWORD', ''),
            'HOST': os.getenv('MSSQL_DB_HOST', ''),
            'PORT': os.getenv('MSSQL_DB_PORT', '1433'),
            'OPTIONS': {
                'driver': os.getenv('MSSQL_ODBC_DRIVER', 'ODBC Driver 17 for SQL Server'),
                'extra_params': 'TrustServerCertificate=yes',
            },
        }
    }
elif _use_mysql:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.getenv('MYSQL_DB_NAME', 'video'),
            'USER': os.getenv('MYSQL_DB_USER', 'root'),
            'PASSWORD': os.getenv('MYSQL_DB_PASSWORD', ''),
            'HOST': os.getenv('MYSQL_DB_HOST', 'localhost'),
            'PORT': os.getenv('MYSQL_DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
            },
        }
    }
else:
    # Default: PostgreSQL
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('POSTGRES_DB',       'cliplens'),
            'USER': os.getenv('POSTGRES_USER',     'postgres'),
            'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
            'HOST': os.getenv('POSTGRES_HOST',     'localhost'),
            'PORT': os.getenv('POSTGRES_PORT',     '5432'),
            'OPTIONS': {
                'connect_timeout': 5,
            },
        }
    }

# ── Cache (Redis — reuses the Celery broker, separate DB index 1) ─────────────
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/1'),
        'TIMEOUT': 300,
        'KEY_PREFIX': 'fs',
    }
}

CACHE_TTL_CATEGORIES    = 60 * 60     # 1 hour  — rarely change
CACHE_TTL_SUBSCRIPTIONS = 60 * 5      # 5 min   — per user
CACHE_TTL_UNREAD_COUNT  = 60          # 1 min   — per user
CACHE_TTL_CHANNEL_INFO  = 60 * 10     # 10 min  — channel metadata

# ── Authentication ────────────────────────────────────────────────────────────
LOGIN_URL          = '/login/'
LOGIN_REDIRECT_URL = '/player/'
LOGOUT_REDIRECT_URL = '/player/'

# Session expires after 8 hours of inactivity; use secure cookie in production
SESSION_COOKIE_AGE    = 60 * 60 * 8   # 8 hours
SESSION_COOKIE_SECURE = not DEBUG      # True in production (HTTPS only)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

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

# ── Logging (dev-friendly) ────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "videos.auth": {"handlers": ["console"], "level": "INFO"},
    },
}

# Static & Media
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise — serves static files efficiently without a separate web server
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS — fully open, all origins, all methods, all headers
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ['DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT']
CORS_ALLOW_HEADERS = ['*']

# REST Framework
REST_FRAMEWORK = {
    # CsrfExemptSessionAuthentication: reads the session cookie so request.user
    # is the logged-in Django user, but skips CSRF token enforcement so that
    # XHR / fetch calls from our own templates don't need to pass a CSRF header.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'videos.authentication.CsrfExemptSessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# FFmpeg — must be installed and on PATH (or set FFMPEG_PATH env var)
FFMPEG_PATH  = os.getenv('FFMPEG_PATH',  '') or 'ffmpeg'
FFPROBE_PATH = os.getenv('FFPROBE_PATH', '') or 'ffprobe'

# ── Celery ────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL         = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND     = 'django-db'          # stores results in celery_taskmeta table
CELERY_CACHE_BACKEND      = 'default'
CELERY_ACCEPT_CONTENT     = ['json']
CELERY_TASK_SERIALIZER    = 'json'
CELERY_RESULT_SERIALIZER  = 'json'
CELERY_TIMEZONE           = 'UTC'
CELERY_TASK_TRACK_STARTED = True
# Route slow processing tasks to a dedicated queue
CELERY_TASK_ROUTES = {
    'videos.tasks.process_video_task':          {'queue': 'processing'},
    'videos.tasks.generate_captions_task':      {'queue': 'captions'},
    'videos.tasks.analyze_video_frames_task':   {'queue': 'processing'},
    'videos.tasks.analyze_photo_task':          {'queue': 'processing'},
    'videos.tasks.extract_audio_tracks_task':   {'queue': 'processing'},
    'videos.tasks.reindex_segments_task':       {'queue': 'default'},
    'videos.tasks.run_diarization_task':        {'queue': 'captions'},
}


# Flower basic authentication
FLOWER_BASIC_AUTH = os.getenv('FLOWER_BASIC_AUTH', 'admin:password')

# ── Frame Analysis / Object Detection (YOLOv8) ───────────────────────────────
# Set FRAME_ANALYSIS_ENABLED=false in .env to skip during development
FRAME_ANALYSIS_ENABLED   = os.getenv('FRAME_ANALYSIS_ENABLED',   'true').lower() == 'true'
FACE_RECOGNITION_ENABLED  = os.getenv('FACE_RECOGNITION_ENABLED',  'true').lower() == 'true'
SCENE_DESCRIPTION_ENABLED = os.getenv('SCENE_DESCRIPTION_ENABLED', 'true').lower() == 'true'
SCENE_CAPTION_MODEL      = os.getenv('SCENE_CAPTION_MODEL', 'blip')       # 'blip' or 'florence2'
CLIP_ENABLED             = os.getenv('CLIP_ENABLED', 'true').lower() == 'true'
CLIP_SIMILARITY_THRESHOLD       = float(os.getenv('CLIP_SIMILARITY_THRESHOLD', '0.24'))   # video frames
CLIP_PHOTO_SIMILARITY_THRESHOLD = float(os.getenv('CLIP_PHOTO_SIMILARITY_THRESHOLD', '0.28'))  # photos (stricter)
FUZZY_SEARCH_ENABLED            = os.getenv('FUZZY_SEARCH_ENABLED', 'true').lower() == 'true'
FUZZY_SEARCH_SIMILARITY_THRESHOLD = float(os.getenv('FUZZY_SEARCH_SIMILARITY_THRESHOLD', '0.35'))  # was 0.22 — too low
FRAME_INTERVAL_SECONDS   = int(os.getenv('FRAME_INTERVAL_SECONDS', '5'))   # 1 frame per N secs
YOLO_MODEL               = os.getenv('YOLO_MODEL', 'yolov8n')              # nano/small/medium

# ── Scene-change frame extraction ─────────────────────────────────────────────
# When SCENE_CHANGE_ENABLED=true, FFmpeg also extracts a frame at every hard cut
# (pixel-level difference > SCENE_CHANGE_THRESHOLD) in addition to the regular
# FRAME_INTERVAL_SECONDS baseline.  Near-duplicate frames (closer than
# SCENE_CHANGE_MIN_GAP seconds to an already-kept frame) are dropped so a cut
# that lands right on the interval doesn't double-process the same instant.
SCENE_CHANGE_ENABLED      = os.getenv('SCENE_CHANGE_ENABLED', 'true').lower() == 'true'
SCENE_CHANGE_THRESHOLD    = float(os.getenv('SCENE_CHANGE_THRESHOLD', '0.35'))   # 0–1; lower = more sensitive
SCENE_CHANGE_MIN_GAP      = float(os.getenv('SCENE_CHANGE_MIN_GAP', '0.5'))      # seconds; dedup window

# ── Face recognition settings ─────────────────────────────────────────────────
# Max crop *images* saved per identity per video (keeps storage bounded).
# All DetectedFace rows are still created; only the N most frontal faces get images.
FACE_MAX_CROPS_PER_VIDEO    = int(os.getenv('FACE_MAX_CROPS_PER_VIDEO', '6'))
# Cosine similarity threshold above which a crop is auto-confirmed (0.0–1.0).
# Higher = stricter; only very confident matches skip manual review.
FACE_AUTO_CONFIRM_THRESHOLD = float(os.getenv('FACE_AUTO_CONFIRM_THRESHOLD', '0.82'))

# Face detection quality gates — applied per-detection before clustering.
# Raising these reduces junk crops (blurry, tiny, side-profile faces).
FACE_MIN_DET_SCORE = float(os.getenv('FACE_MIN_DET_SCORE', '0.20'))  # InsightFace confidence 0–1
FACE_MIN_AREA_PX   = int(os.getenv('FACE_MIN_AREA_PX',   '200'))    # min bbox area in pixels (60×60)
FACE_MAX_YAW_DEG   = float(os.getenv('FACE_MAX_YAW_DEG', '80'))      # max head yaw before skipping

# Cosine similarity threshold for cross-video speaker matching (0.0–1.0).
# A new speaker embedding is matched to an existing SpeakerIdentity when
# cosine_sim >= threshold.  0.75 is a good balance for wespeaker-resnet34.
# Raise to 0.85+ if you get false merges; lower to 0.65 if same person
# isn't being matched across videos.
SPEAKER_MATCH_THRESHOLD = float(os.getenv('SPEAKER_MATCH_THRESHOLD', '0.75'))

# ── Captions / Whisper ────────────────────────────────────────────────────────
# Model size: tiny | base | small | medium | large-v2 | large-v3
# Larger = more accurate but slower. 'base' is a good dev default.
WHISPER_MODEL_SIZE  = os.getenv('WHISPER_MODEL_SIZE', 'base')
WHISPER_DEVICE      = os.getenv('WHISPER_DEVICE', 'cpu')   # 'cuda' if GPU available
WHISPER_COMPUTE_TYPE= os.getenv('WHISPER_COMPUTE_TYPE', 'int8')
AUTO_CAPTION_ON_UPLOAD = os.getenv('AUTO_CAPTION_ON_UPLOAD', 'true').lower() == 'true'

# ── Speaker Diarization (pyannote.audio) ──────────────────────────────────────
# Required for the "Run Diarization" feature on videos.
# Get a free token at https://huggingface.co/settings/tokens
# Then accept terms at: https://huggingface.co/pyannote/speaker-diarization-3.1
HF_TOKEN = os.getenv('HF_TOKEN', '')
# Set to 1 after initial model download to skip HuggingFace version-check pings (eliminates timeout noise)
if os.getenv('HF_HUB_OFFLINE', '0') == '1':
    os.environ['HF_HUB_OFFLINE'] = '1'

# HLS segment duration in seconds
HLS_SEGMENT_DURATION = int(os.getenv('HLS_SEGMENT_DURATION', 6))

# ── Multi-resolution / Adaptive Bitrate HLS ───────────────────────────────────
# Toggle on/off via .env
HLS_MULTI_QUALITY = os.getenv('HLS_MULTI_QUALITY', 'true').lower() == 'true'

# Quality ladder — list of target heights from .env (e.g. "1080,720,480,360")
# Each entry matches a row in the QUALITY_LADDER table inside services.py.
HLS_QUALITIES = [
    int(h.strip())
    for h in os.getenv('HLS_QUALITIES', '1080,720,480,360').split(',')
    if h.strip().isdigit()
]

# ── Django Debug Toolbar (dev only) ──────────────────────────────────────────
if DEBUG:
    INTERNAL_IPS = ['127.0.0.1', '::1']
    DEBUG_TOOLBAR_CONFIG = {
        # Show toolbar for all requests from INTERNAL_IPS, not just HTML responses
        'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG,
        # Panels to enable — all the useful ones
        'PANELS': [
            'debug_toolbar.panels.history.HistoryPanel',
            'debug_toolbar.panels.versions.VersionsPanel',
            'debug_toolbar.panels.timer.TimerPanel',
            'debug_toolbar.panels.settings.SettingsPanel',
            'debug_toolbar.panels.headers.HeadersPanel',
            'debug_toolbar.panels.request.RequestPanel',
            'debug_toolbar.panels.sql.SQLPanel',
            'debug_toolbar.panels.staticfiles.StaticFilesPanel',
            'debug_toolbar.panels.templates.TemplatesPanel',
            'debug_toolbar.panels.cache.CachePanel',
            'debug_toolbar.panels.signals.SignalsPanel',
            'debug_toolbar.panels.logging.LoggingPanel',
            'debug_toolbar.panels.redirects.RedirectsPanel',
            'debug_toolbar.panels.profiling.ProfilingPanel',
        ],
        # Show duplicate SQL queries highlighted in red
        'SHOW_COLLAPSED': False,
    }
