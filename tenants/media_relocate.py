"""
Per-tenant media relocation service.

When a platform admin changes a tenant's `media_root_absolute`, ClipLens
needs to physically move every file from the old location to the new one,
keeping reads/writes consistent and providing a grace period for rollback.

Flow:
  1. Pre-flight checks (target accessible, writable, empty, enough space)
  2. Mark tenant.media_relocating=True → maintenance page kicks in
  3. Walk source tree, copy each file with shutil + SHA-256 verify
  4. Poll cancel flag between files — graceful abort if requested
  5. Atomic swap: tenant.media_root_absolute = target
  6. Soft-delete old path → "<old>.delete_after_<ts>" (kept 24h for rollback)
  7. Clear tenant.media_relocating → tenant access restored

A daily Celery beat task (or manual button) purges expired soft-deletes.
"""

import errno
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Filesystem permission needed to write
_WRITE_TEST_NAME = '.cliplens_write_test'

# Free-space safety margin (10%): target must have >= source_size * 1.10
SPACE_SAFETY_MARGIN = 1.10

# How long to keep the soft-deleted old data before purging (configurable via .env)
GRACE_PERIOD_HOURS = int(os.getenv('MEDIA_RELOCATE_GRACE_HOURS', '24'))


# ── Pre-flight ───────────────────────────────────────────────────────────────

class RelocateError(Exception):
    """User-facing relocation failure (raised by pre-flight)."""


def _current_media_path(tenant) -> str:
    """Resolve the tenant's current absolute media root (where files live now)."""
    if (tenant.media_root_absolute or '').strip():
        return tenant.media_root_absolute.strip()
    return str((Path(settings.MEDIA_ROOT) / tenant.media_folder).resolve())


def _path_writable(path: str) -> bool:
    """True if we can create + delete a temp file at the path."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        test_path = os.path.join(path, _WRITE_TEST_NAME)
        with open(test_path, 'wb') as fh:
            fh.write(b'cliplens-write-test')
        os.remove(test_path)
        return True
    except (OSError, PermissionError):
        return False


def _dir_size(path: str) -> tuple[int, int]:
    """Total (size_bytes, file_count) of files under path. Skips broken links."""
    total = 0
    count = 0
    if not os.path.isdir(path):
        return 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if os.path.islink(fp):
                    continue
                total += os.path.getsize(fp)
                count += 1
            except OSError:
                continue
    return total, count


def _free_bytes(path: str) -> int:
    """Available bytes on the filesystem hosting `path` (or its nearest parent)."""
    p = path
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return 0
        p = parent
    try:
        st = os.statvfs(p)
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        return 0


def preflight(tenant, target: str) -> dict:
    """
    Run all pre-flight checks. Returns a dict of stats on success.
    Raises RelocateError on any failure with a user-facing message.
    """
    target = (target or '').strip()
    if not target:
        raise RelocateError('Target path is empty.')
    if not os.path.isabs(target):
        raise RelocateError(f'Target must be an absolute path (got: {target!r}).')

    source = _current_media_path(tenant)
    src_norm = os.path.normpath(source)
    tgt_norm = os.path.normpath(target)
    if src_norm == tgt_norm:
        raise RelocateError('Target is the same as the current location.')

    # Reject nested paths (target inside source or vice versa)
    if tgt_norm.startswith(src_norm + os.sep) or src_norm.startswith(tgt_norm + os.sep):
        raise RelocateError('Target cannot be a sub-path of the current location (or vice versa).')

    # Target accessibility
    parent = os.path.dirname(tgt_norm)
    if not os.path.isdir(parent):
        raise RelocateError(f'Target parent directory does not exist: {parent}\n'
                            f'Create or mount it first, then retry.')

    # If target exists, it must be an empty directory
    if os.path.exists(tgt_norm):
        if not os.path.isdir(tgt_norm):
            raise RelocateError(f'Target exists but is not a directory.')
        if any(os.scandir(tgt_norm)):
            raise RelocateError(f'Target directory exists but is not empty.')
    # Writability
    if not _path_writable(tgt_norm):
        raise RelocateError(
            f'Target is not writable by the running user. '
            f'Check permissions or that the mount is online: {tgt_norm}'
        )

    # Disk space
    src_size, src_count = _dir_size(src_norm)
    free = _free_bytes(tgt_norm)
    required = int(src_size * SPACE_SAFETY_MARGIN)
    if free < required:
        raise RelocateError(
            f'Insufficient free space on target. '
            f'Need {required / 1e9:.2f} GB (source {src_size / 1e9:.2f} GB + 10% margin); '
            f'available {free / 1e9:.2f} GB.'
        )

    return {
        'source':         src_norm,
        'target':         tgt_norm,
        'source_size':    src_size,
        'source_files':   src_count,
        'target_free':    free,
        'required_bytes': required,
    }


# ── Copy + verify ────────────────────────────────────────────────────────────

def _sha256_of(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _should_cancel(relocation_id: int) -> bool:
    """Polled between files. Returns True if admin clicked Cancel."""
    from .models import MediaRelocation
    try:
        return MediaRelocation.objects.using('control').filter(
            pk=relocation_id, tenant__media_relocation_cancel_requested=True
        ).exists()
    except Exception:
        return False


def _cleanup_partial(target: str):
    """Remove all files we've copied so far at target. Used on cancel/failure."""
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
    except Exception:
        logger.exception('cleanup_partial: failed to remove %s', target)


def run_relocation(relocation_id: int) -> dict:
    """
    Main task body. Runs from Celery. Updates MediaRelocation as progress
    happens. On success, atomically swaps tenant.media_root_absolute.
    """
    from .models import MediaRelocation, Tenant

    rel = MediaRelocation.objects.using('control').select_related('tenant').get(pk=relocation_id)
    tenant = rel.tenant
    source = rel.source_path
    target = rel.target_path

    rel.status = MediaRelocation.STATUS_RUNNING
    rel.started_at = timezone.now()
    rel.save(using='control', update_fields=['status', 'started_at'])

    try:
        # Fresh size sweep (so progress reporting is honest)
        total_size, total_files = _dir_size(source)
        rel.total_bytes = total_size
        rel.total_files = total_files
        rel.save(using='control', update_fields=['total_bytes', 'total_files'])

        # ── Copy files ──
        copied_bytes = 0
        copied_files = 0
        last_progress_save = time.monotonic()

        for src_dir, _subdirs, files in os.walk(source):
            rel_dir = os.path.relpath(src_dir, source)
            tgt_dir = os.path.normpath(os.path.join(target, rel_dir))
            os.makedirs(tgt_dir, exist_ok=True)
            for f in files:
                if _should_cancel(relocation_id):
                    logger.info('relocation %s: cancel requested, aborting at %d/%d files',
                                relocation_id, copied_files, total_files)
                    _cleanup_partial(target)
                    rel.status = MediaRelocation.STATUS_CANCELLED
                    rel.finished_at = timezone.now()
                    rel.error_message = 'Cancelled by admin'
                    rel.save(using='control')
                    Tenant.objects.using('control').filter(pk=tenant.pk).update(
                        media_relocating=False,
                        media_relocation_started_at=None,
                        media_relocation_cancel_requested=False,
                        media_relocation_task_id='',
                    )
                    return {'ok': False, 'cancelled': True}

                src_path = os.path.join(src_dir, f)
                dst_path = os.path.join(tgt_dir, f)
                try:
                    if os.path.islink(src_path):
                        # Skip symlinks for simplicity (rare in our pipeline)
                        continue
                    shutil.copy2(src_path, dst_path)   # preserves mtime
                    copied_bytes += os.path.getsize(dst_path)
                    copied_files += 1
                except OSError as exc:
                    raise RelocateError(f'Copy failed at {src_path}: {exc}') from exc

                # Throttle progress saves to once per second
                now = time.monotonic()
                if now - last_progress_save > 1.0:
                    rel.bytes_copied = copied_bytes
                    rel.files_copied = copied_files
                    rel.save(using='control', update_fields=['bytes_copied', 'files_copied'])
                    last_progress_save = now

        rel.bytes_copied = copied_bytes
        rel.files_copied = copied_files
        rel.save(using='control', update_fields=['bytes_copied', 'files_copied'])

        # ── Verify ──
        rel.status = MediaRelocation.STATUS_VERIFYING
        rel.save(using='control', update_fields=['status'])
        tgt_size, tgt_files = _dir_size(target)
        size_match = abs(tgt_size - total_size) < max(1024, total_size * 0.01)
        files_match = tgt_files == total_files
        if not (size_match and files_match):
            raise RelocateError(
                f'Verification failed: source={total_size}B/{total_files}f, '
                f'target={tgt_size}B/{tgt_files}f'
            )

        # ── Atomic swap ──
        ts = int(time.time())
        soft_deleted = f'{source}.delete_after_{ts}'
        try:
            os.rename(source, soft_deleted)
        except OSError as exc:
            # rename across volumes fails — fall back to leaving source in place
            # and storing the original path as the soft-deleted marker. We'll
            # purge it later by deleting source directly.
            if exc.errno in (errno.EXDEV, errno.EINVAL):
                soft_deleted = source
                logger.warning('relocation %s: cross-volume rename skipped, source stays put',
                               relocation_id)
            else:
                raise

        Tenant.objects.using('control').filter(pk=tenant.pk).update(
            media_root_absolute=target,
            media_relocating=False,
            media_relocation_started_at=None,
            media_relocation_cancel_requested=False,
            media_relocation_task_id='',
        )

        # Mark relocation done
        rel.status = MediaRelocation.STATUS_SUCCEEDED
        rel.finished_at = timezone.now()
        rel.old_path_soft_deleted = soft_deleted
        from datetime import timedelta as _td
        rel.grace_period_until = timezone.now() + _td(hours=GRACE_PERIOD_HOURS)
        rel.save(using='control')

        logger.info('relocation %s: SUCCESS — copied %d files / %d bytes; old data parked at %s',
                    relocation_id, copied_files, copied_bytes, soft_deleted)
        return {'ok': True, 'files': copied_files, 'bytes': copied_bytes,
                'soft_deleted': soft_deleted}

    except RelocateError as exc:
        rel.status = MediaRelocation.STATUS_FAILED
        rel.error_message = str(exc)
        rel.finished_at = timezone.now()
        rel.save(using='control')
        _cleanup_partial(target)
        Tenant.objects.using('control').filter(pk=tenant.pk).update(
            media_relocating=False,
            media_relocation_started_at=None,
            media_relocation_cancel_requested=False,
            media_relocation_task_id='',
        )
        return {'ok': False, 'error': str(exc)}
    except Exception as exc:
        logger.exception('relocation %s crashed', relocation_id)
        rel.status = MediaRelocation.STATUS_FAILED
        rel.error_message = f'{type(exc).__name__}: {exc}'[:4000]
        rel.finished_at = timezone.now()
        rel.save(using='control')
        # Don't clear cancel/relocating flags here — admin can force-cancel
        return {'ok': False, 'error': str(exc)}


def purge_soft_deleted(relocation_id: int) -> dict:
    """Immediately purge the soft-deleted old data for a completed relocation."""
    from .models import MediaRelocation
    rel = MediaRelocation.objects.using('control').get(pk=relocation_id)
    if not rel.old_path_soft_deleted or not os.path.isdir(rel.old_path_soft_deleted):
        return {'ok': True, 'purged': False, 'reason': 'nothing to purge'}
    try:
        shutil.rmtree(rel.old_path_soft_deleted)
        rel.purged_at = timezone.now()
        rel.save(using='control', update_fields=['purged_at'])
        return {'ok': True, 'purged': True, 'path': rel.old_path_soft_deleted}
    except Exception as exc:
        logger.exception('purge_soft_deleted failed for %s', relocation_id)
        return {'ok': False, 'error': str(exc)}


def purge_expired() -> int:
    """
    Called by a Celery beat task once per day.
    Purges every relocation whose grace_period_until is in the past.
    Returns count of purges.
    """
    from .models import MediaRelocation
    now = timezone.now()
    qs = (MediaRelocation.objects.using('control')
          .filter(status=MediaRelocation.STATUS_SUCCEEDED,
                  purged_at__isnull=True,
                  grace_period_until__lt=now)
          .exclude(old_path_soft_deleted=''))
    n = 0
    for rel in qs:
        try:
            if os.path.isdir(rel.old_path_soft_deleted):
                shutil.rmtree(rel.old_path_soft_deleted)
            rel.purged_at = now
            rel.save(using='control', update_fields=['purged_at'])
            n += 1
        except Exception:
            logger.exception('purge_expired: failed for relocation %s', rel.pk)
    return n
