"""
Celery tenant context wiring
─────────────────────────────
Connects to Celery's task_prerun / task_postrun signals so that any task
dispatched with tenant_slug='orga' automatically gets the correct DB alias
and media root set before it runs, and cleared when it finishes.

Usage in views (dispatch a tenant-aware task):
    process_video_task.apply_async(
        args=[str(video.id)],
        kwargs={'tenant_slug': request.tenant.slug},
        queue='processing',
    )

Usage in tasks that chain to other tasks:
    generate_captions_task.apply_async(
        args=[video_id],
        kwargs={'tenant_slug': tenant_slug},   # forward it
        queue='captions',
    )

The task function itself does NOT need to call set_db — the signal handles it.
Tasks just need to accept **kwargs or an explicit tenant_slug= parameter.

Phase 4 addition: task_prerun records the start time; task_postrun/task_failure
compute elapsed minutes and write a UsageEvent to the control DB so the
platform dashboard can track AI processing time per tenant.
"""

import logging
import threading
import time
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Thread-local store for task start times: {task_id: (start_ts, tenant_slug)}
_task_timing = threading.local()

# Map Celery task names → metering event types
# Maps every metered Celery task name → UsageEvent.event_type.
# Unknown task names fall back to 'video_processing' in _log_task_duration.
# Tasks intentionally excluded (no AI compute): reindex_segments_task, run_live_ffmpeg.
_TASK_EVENT_TYPES = {
    # ── Video pipeline ────────────────────────────────────────────────────────
    'videos.tasks.process_video_task':            'video_processing',   # FFmpeg HLS + thumbnail
    'videos.tasks.generate_seek_thumbnails_task': 'video_processing',   # FFmpeg sprite sheet
    'videos.tasks.extract_audio_tracks_task':     'video_processing',   # FFmpeg audio tracks
    'videos.tasks.upscale_video_task':            'video_processing',   # Lanczos upscale
    'videos.tasks.encode_single_quality_task':    'video_processing',   # On-demand rendition encode
    'videos.tasks.generate_video_summary_task':   'video_processing',   # Ollama LLM summary
    # ── AI analysis ───────────────────────────────────────────────────────────
    'videos.tasks.analyze_video_frames_task':     'frame_analysis',     # YOLO + InsightFace + BLIP + CLIP
    'videos.tasks.analyze_photo_task':            'photo_processing',   # YOLO + InsightFace + BLIP + CLIP
    'videos.tasks.upscale_photo_task':            'photo_processing',   # Lanczos upscale
    # ── Speech & language ─────────────────────────────────────────────────────
    'videos.tasks.generate_captions_task':        'captions',           # Whisper transcription
    'videos.tasks.translate_subtitles_task':      'translation',        # NLLB-200 translation
    'videos.tasks.run_diarization_task':          'diarization',        # pyannote speaker diarization
    # ── Audio ─────────────────────────────────────────────────────────────────
    'videos.tasks.detect_audio_events_task':      'audio_events',       # PANNs CNN14 event detection
}


def setup_tenant_context(tenant_slug: str) -> None:
    """Set DB alias + media root for the given tenant slug in this thread."""
    if not tenant_slug:
        return
    try:
        from .models import Tenant
        from .db_router import set_db
        from .storage import set_media_root
        from .provisioning import _register_db_alias

        tenant = Tenant.objects.using('control').get(slug=tenant_slug, is_active=True)
        _register_db_alias(tenant.db_name)
        set_db(tenant.db_name)

        # Honour per-tenant custom absolute path if set
        if getattr(tenant, 'media_root_absolute', '').strip():
            media_abs = tenant.media_root_absolute.strip()
        else:
            media_abs = str((Path(settings.MEDIA_ROOT) / tenant.media_folder).resolve())
        # URL prefix is ALWAYS tenants/<slug> regardless of disk location.
        url_prefix = (tenant.media_folder or '').strip('/') or f'tenants/{tenant.slug}'
        set_media_root(media_abs, url_prefix=url_prefix)

        logger.debug("Celery tenant context set: slug=%s db=%s", tenant_slug, tenant.db_name)
    except Exception:
        logger.exception("Failed to set tenant context for slug=%s", tenant_slug)


def clear_tenant_context() -> None:
    """Clear DB alias + media root after a task finishes."""
    try:
        from .db_router import clear_db
        from .storage import clear_media_root
        clear_db()
        clear_media_root()
    except Exception:
        pass


def _log_task_duration(task_id: str, task_name: str, tenant_slug: str, start_ts: float) -> None:
    """Write a UsageEvent for AI processing minutes to the control DB."""
    if not tenant_slug:
        return
    elapsed_minutes = (time.monotonic() - start_ts) / 60.0
    if elapsed_minutes < 0.001:
        return  # ignore sub-second tasks (retries, fast no-ops)
    event_type = _TASK_EVENT_TYPES.get(task_name, 'video_processing')
    try:
        from .metering import log_ai_minutes
        log_ai_minutes(
            tenant_slug=tenant_slug,
            minutes=elapsed_minutes,
            event_type=event_type,
            task_id=task_id,
        )
        logger.debug(
            "metering: logged %.2f min for task=%s tenant=%s",
            elapsed_minutes, task_name, tenant_slug,
        )
    except Exception:
        logger.exception("metering: failed to log AI minutes for task=%s", task_id)


def _on_task_prerun(task_id, task, args, kwargs, **_):
    slug = (kwargs or {}).get('tenant_slug', '')
    if slug:
        setup_tenant_context(slug)
    # Record start time for metering (always, even for unknown slugs)
    if not hasattr(_task_timing, 'tasks'):
        _task_timing.tasks = {}
    _task_timing.tasks[task_id] = (time.monotonic(), slug)


def _on_task_postrun(task_id, task, args, kwargs, retval, state, **_):
    slug = (kwargs or {}).get('tenant_slug', '')
    timing = getattr(_task_timing, 'tasks', {}).pop(task_id, None)
    if timing and slug:
        start_ts, _ = timing
        _log_task_duration(task_id, task.name, slug, start_ts)
    clear_tenant_context()


def _on_task_failure(task_id, exception, args, kwargs, **extra):
    slug = (kwargs or {}).get('tenant_slug', '')
    timing = getattr(_task_timing, 'tasks', {}).pop(task_id, None)
    if timing and slug:
        start_ts, _ = timing
        # Still log partial usage on failure — AI compute was consumed
        _log_task_duration(task_id, '', slug, start_ts)

    # ── Record the failure in the control-plane FailedTask log ──────────────
    # Visible to the platform owner at /tasks/failed/. Must never raise —
    # a broken log writer should not mask the original task failure.
    try:
        import traceback as _tb
        from .models import FailedTask

        sender = extra.get('sender')
        task_name = getattr(sender, 'name', '') or extra.get('task', '') or ''
        # Celery passes einfo with the formatted traceback when available
        einfo = extra.get('einfo')
        tb_text = str(einfo) if einfo else _tb.format_exc()

        FailedTask.objects.using('control').create(
            task_name=task_name[:200],
            task_id=task_id or '',
            tenant_slug=slug,
            queue=(getattr(getattr(sender, 'request', None), 'delivery_info', None) or {}).get('routing_key', '')
                  if sender else '',
            args_snippet=repr(args)[:500],
            error_message=f'{type(exception).__name__}: {exception}'[:2000],
            traceback=tb_text[-8000:],
        )
    except Exception:
        logger.exception('FailedTask log write failed for task %s', task_id)

    clear_tenant_context()


def connect_celery_signals() -> None:
    """
    Connect task_prerun and task_postrun signals.
    Called once from TenantsConfig.ready() when MULTI_TENANT=True.

    Handlers are defined at module level (not as inner functions) so that
    Celery's weak-reference signal system does not garbage-collect them
    immediately after connect_celery_signals() returns.
    """
    from celery.signals import task_prerun, task_postrun, task_failure

    task_prerun.connect(_on_task_prerun,   weak=False)
    task_postrun.connect(_on_task_postrun, weak=False)
    task_failure.connect(_on_task_failure, weak=False)

    logger.info("Celery tenant context signals connected.")
