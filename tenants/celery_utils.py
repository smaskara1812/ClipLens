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
_TASK_EVENT_TYPES = {
    'videos.tasks.process_video_task':        'video_processing',
    'videos.tasks.generate_captions_task':    'captions',
    'videos.tasks.analyze_video_frames_task': 'frame_analysis',
    'videos.tasks.analyze_photo_task':        'photo_processing',
    'videos.tasks.translate_subtitles_task':  'translation',
    'videos.tasks.run_diarization_task':      'diarization',
    'videos.tasks.detect_audio_events_task':  'audio_events',
    'videos.tasks.upscale_video_task':        'video_processing',
    'videos.tasks.upscale_photo_task':        'photo_processing',
    'videos.tasks.generate_video_summary_task': 'video_processing',
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

        media_abs = str((Path(settings.MEDIA_ROOT) / tenant.media_folder).resolve())
        set_media_root(media_abs)

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


def connect_celery_signals() -> None:
    """
    Connect task_prerun and task_postrun signals.
    Called once from TenantsConfig.ready() when MULTI_TENANT=True.
    """
    from celery.signals import task_prerun, task_postrun, task_failure

    @task_prerun.connect
    def _on_task_prerun(task_id, task, args, kwargs, **_):
        slug = (kwargs or {}).get('tenant_slug', '')
        if slug:
            setup_tenant_context(slug)
        # Record start time for metering (always, even for unknown slugs)
        if not hasattr(_task_timing, 'tasks'):
            _task_timing.tasks = {}
        _task_timing.tasks[task_id] = (time.monotonic(), slug)

    @task_postrun.connect
    def _on_task_postrun(task_id, task, args, kwargs, retval, state, **_):
        slug = (kwargs or {}).get('tenant_slug', '')
        timing = getattr(_task_timing, 'tasks', {}).pop(task_id, None)
        if timing and slug:
            start_ts, _ = timing
            _log_task_duration(task_id, task.name, slug, start_ts)
        clear_tenant_context()

    @task_failure.connect
    def _on_task_failure(task_id, exception, args, kwargs, **_):
        slug = (kwargs or {}).get('tenant_slug', '')
        timing = getattr(_task_timing, 'tasks', {}).pop(task_id, None)
        if timing and slug:
            start_ts, _ = timing
            # Still log partial usage on failure — AI compute was consumed
            try:
                from celery import current_app
                task_name = ''  # name not available in failure signal kwargs
                _log_task_duration(task_id, task_name, slug, start_ts)
            except Exception:
                pass
        clear_tenant_context()

    logger.info("Celery tenant context signals connected.")
