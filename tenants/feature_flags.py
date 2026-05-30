"""
Per-tenant AI feature gating
─────────────────────────────
Each AI feature is controlled by two switches:
  1. A global env var (settings.FACE_RECOGNITION_ENABLED, etc.) — operator-level.
  2. A per-tenant boolean on the Tenant model — customer-level.

A feature runs only when BOTH are True. Either side off → skip.

Management commands (run_yolo, propagate_identities, etc.) bypass these
checks entirely — they are operator-initiated and trust the operator's intent.
The feature flags only gate the AUTOMATIC pipeline that runs on upload.
"""

import logging
from django.conf import settings

logger = logging.getLogger(__name__)


# Map feature name → (Tenant field, settings env var)
# settings env var = None means there's no global toggle (only the per-tenant one)
FEATURES = {
    'face_recognition':  ('feature_face_recognition',  'FACE_RECOGNITION_ENABLED'),
    'speech_to_text':    ('feature_speech_to_text',    None),                          # always runs if AUTO_CAPTION_ON_UPLOAD
    'translation':       ('feature_translation',       'TRANSLATION_ENABLED'),
    'diarization':       ('feature_diarization',       None),                          # manually triggered
    'audio_events':      ('feature_audio_events',      'AUDIO_EVENTS_ENABLED'),
    'scene_description': ('feature_scene_description', 'SCENE_DESCRIPTION_ENABLED'),
    'object_detection':  ('feature_object_detection',  None),                          # part of frame_analysis
    'clip_embeddings':   ('feature_clip_embeddings',   'CLIP_ENABLED'),
    'video_summary':     ('feature_video_summary',     'USE_OLLAMA'),
    'auto_captions':     ('feature_auto_captions',     'AUTO_CAPTION_ON_UPLOAD'),
}


def _global_enabled(env_var: str) -> bool:
    """Read the global env flag. None = no global toggle, treated as True."""
    if env_var is None:
        return True
    return bool(getattr(settings, env_var, True))


def is_feature_enabled(tenant_slug: str, feature_name: str) -> bool:
    """
    Return True if the given AI feature should run for the given tenant.

    In single-tenant mode (no slug or no Tenant row found), only the global
    env flag applies — current behaviour preserved.
    """
    if feature_name not in FEATURES:
        logger.warning("Unknown feature name: %s — defaulting to enabled", feature_name)
        return True

    tenant_field, env_var = FEATURES[feature_name]

    # Global env check first (cheap)
    if not _global_enabled(env_var):
        return False

    # Per-tenant override (skipped when no tenant)
    if not tenant_slug:
        return True

    try:
        from .models import Tenant
        tenant = Tenant.objects.using('control').only(tenant_field).get(slug=tenant_slug)
        return bool(getattr(tenant, tenant_field, True))
    except Exception:
        # Unknown tenant or DB hiccup → fail-open (let the task run)
        return True


def skip_if_disabled(tenant_slug: str, feature_name: str) -> bool:
    """
    Convenience for use at the top of a Celery task:

        if skip_if_disabled(tenant_slug, 'face_recognition'):
            return {'skipped': 'feature_disabled'}

    Returns True (caller should bail) when the feature is OFF for this tenant.
    Logs an info line so the operator can see disabled-skips in the worker log.
    """
    if not is_feature_enabled(tenant_slug, feature_name):
        logger.info(
            "Skipping %s for tenant=%s (disabled by tenant or global flag)",
            feature_name, tenant_slug or '(none)',
        )
        return True
    return False
