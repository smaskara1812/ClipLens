from pathlib import Path
import os
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'freestream-dev-secret-key-2024')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

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

ROOT_URLCONF = 'freestream.urls'

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

WSGI_APPLICATION = 'freestream.wsgi.application'

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
            'NAME': os.getenv('POSTGRES_DB',       'freestream'),
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
FFMPEG_PATH = os.getenv('FFMPEG_PATH', 'ffmpeg')
FFPROBE_PATH = os.getenv('FFPROBE_PATH', 'ffprobe')

# ── Site URL — used in templates and API responses for absolute URLs ──────────
# Change SITE_URL in .env — do NOT hardcode here.
SITE_URL = os.getenv('SITE_URL', 'http://localhost:8000').rstrip('/')

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
}

# ── Frame Analysis / Object Detection (YOLOv8) ───────────────────────────────
# Set FRAME_ANALYSIS_ENABLED=false in .env to skip during development
FRAME_ANALYSIS_ENABLED   = os.getenv('FRAME_ANALYSIS_ENABLED',   'true').lower() == 'true'
FACE_RECOGNITION_ENABLED  = os.getenv('FACE_RECOGNITION_ENABLED',  'true').lower() == 'true'
SCENE_DESCRIPTION_ENABLED = os.getenv('SCENE_DESCRIPTION_ENABLED', 'true').lower() == 'true'
SCENE_CAPTION_MODEL      = os.getenv('SCENE_CAPTION_MODEL', 'blip')       # 'blip' or 'florence2'
CLIP_ENABLED             = os.getenv('CLIP_ENABLED', 'true').lower() == 'true'
CLIP_SIMILARITY_THRESHOLD = float(os.getenv('CLIP_SIMILARITY_THRESHOLD', '0.20'))
FUZZY_SEARCH_ENABLED      = os.getenv('FUZZY_SEARCH_ENABLED', 'true').lower() == 'true'
FUZZY_SEARCH_SIMILARITY_THRESHOLD = float(os.getenv('FUZZY_SEARCH_SIMILARITY_THRESHOLD', '0.22'))
FRAME_INTERVAL_SECONDS   = int(os.getenv('FRAME_INTERVAL_SECONDS', '5'))   # 1 frame per N secs
YOLO_MODEL               = os.getenv('YOLO_MODEL', 'yolov8n')              # nano/small/medium

# ── Face recognition settings ─────────────────────────────────────────────────
# Max crop *images* saved per identity per video (keeps storage bounded).
# All DetectedFace rows are still created; only the N most frontal faces get images.
FACE_MAX_CROPS_PER_VIDEO    = int(os.getenv('FACE_MAX_CROPS_PER_VIDEO', '6'))
# Cosine similarity threshold above which a crop is auto-confirmed (0.0–1.0).
# Higher = stricter; only very confident matches skip manual review.
FACE_AUTO_CONFIRM_THRESHOLD = float(os.getenv('FACE_AUTO_CONFIRM_THRESHOLD', '0.82'))

# ── Captions / Whisper ────────────────────────────────────────────────────────
# Model size: tiny | base | small | medium | large-v2 | large-v3
# Larger = more accurate but slower. 'base' is a good dev default.
WHISPER_MODEL_SIZE  = os.getenv('WHISPER_MODEL_SIZE', 'base')
WHISPER_DEVICE      = os.getenv('WHISPER_DEVICE', 'cpu')   # 'cuda' if GPU available
WHISPER_COMPUTE_TYPE= os.getenv('WHISPER_COMPUTE_TYPE', 'int8')
AUTO_CAPTION_ON_UPLOAD = os.getenv('AUTO_CAPTION_ON_UPLOAD', 'true').lower() == 'true'

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
