"""Activity logging helper.

Usage:
    from .activity import log_activity, snapshot_fields, diff_snapshots

    # Before an update:
    before = snapshot_fields(video, ['title', 'description', 'visibility'])
    # ... apply changes ...
    after  = snapshot_fields(video, ['title', 'description', 'visibility'])
    log_activity(request, 'update', target=video,
                 changes=diff_snapshots(before, after),
                 verb='updated video')

Design notes:
- Failures here must never break the request flow. All helpers swallow
  exceptions (logging them) so a buggy audit path can't take the site down.
- We snapshot a human label (title / name / username) so log rows stay
  informative even after the target is deleted.
"""
from __future__ import annotations

import decimal
import logging
import uuid
from typing import Any, Iterable, Mapping, Optional

from django.db import models

logger = logging.getLogger(__name__)


# -------------------- Request context helpers --------------------

def _ip_from_request(request) -> Optional[str]:
    if request is None:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        # first entry is the client
        return xff.split(',')[0].strip()[:45] or None
    return (request.META.get('REMOTE_ADDR') or '').strip()[:45] or None


def _user_agent(request) -> str:
    if request is None:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:500]


# -------------------- Target label/type helpers --------------------

def _label_for(obj: Any) -> str:
    """Best-effort human label for an arbitrary target instance."""
    if obj is None:
        return ''
    for attr in ('title', 'name', 'slug', 'username', 'label'):
        v = getattr(obj, attr, None)
        if v:
            return str(v)[:500]
    try:
        return str(obj)[:500]
    except Exception:
        return ''


def _type_for(obj: Any) -> str:
    if obj is None:
        return ''
    try:
        return obj.__class__.__name__[:64]
    except Exception:
        return ''


def _id_for(obj: Any) -> str:
    if obj is None:
        return ''
    pk = getattr(obj, 'pk', None)
    return '' if pk is None else str(pk)[:64]


# -------------------- JSON-safe serialization --------------------

def _make_serializable(v: Any) -> Any:
    """Recursively coerce any value to a JSON-native type.

    Handles: UUID, Decimal, datetime/date/time, Django Model instances,
    and nested dicts/lists.  Anything else becomes str().
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if hasattr(v, 'isoformat'):          # datetime, date, time
        return v.isoformat()
    if isinstance(v, models.Model):
        pk = getattr(v, 'pk', None)
        return {
            '_model': v.__class__.__name__,
            'id': _make_serializable(pk),
            'label': _label_for(v),
        }
    if isinstance(v, dict):
        return {str(k): _make_serializable(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_make_serializable(i) for i in v]
    # Fallback: coerce to string
    try:
        return str(v)
    except Exception:
        return None


# -------------------- Diff helpers --------------------

def snapshot_fields(obj: Any, fields: Iterable[str]) -> dict:
    """Capture current values of `fields` from obj; FK fields stored as repr/id."""
    out: dict = {}
    if obj is None:
        return out
    for f in fields:
        try:
            v = getattr(obj, f, None)
            out[f] = _make_serializable(v)
        except Exception:
            out[f] = None
    return out


def diff_snapshots(before: Mapping, after: Mapping) -> dict:
    """Return {field: {before, after}} only for fields whose values differ."""
    out: dict = {}
    keys = set(before or {}) | set(after or {})
    for k in keys:
        b = before.get(k) if before else None
        a = after.get(k) if after else None
        if b != a:
            out[k] = {'before': b, 'after': a}
    return out


# -------------------- Main logger --------------------

def log_activity(
    request,
    action: str,
    *,
    target: Any = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    target_label: Optional[str] = None,
    changes: Optional[Mapping] = None,
    verb: str = '',
    metadata: Optional[Mapping] = None,
    actor=None,
) -> None:
    """Append a row to the ActivityLog. Never raises."""
    try:
        # Lazy import to avoid circular model imports at startup
        from .models import ActivityLog

        if actor is None and request is not None:
            u = getattr(request, 'user', None)
            if u is not None and getattr(u, 'is_authenticated', False):
                actor = u

        actor_username = ''
        if actor is not None:
            actor_username = getattr(actor, 'username', '') or ''

        if target is not None:
            if not target_type:
                target_type = _type_for(target)
            if not target_id:
                target_id = _id_for(target)
            if not target_label:
                target_label = _label_for(target)

        ActivityLog.objects.create(
            actor=actor if actor and getattr(actor, 'pk', None) else None,
            actor_username=(actor_username or '')[:150],
            action=action[:32] if action else ActivityLog.ACTION_OTHER,
            verb=(verb or '')[:140],
            target_type=(target_type or '')[:64],
            target_id=(str(target_id) if target_id is not None else '')[:64],
            target_label=(target_label or '')[:500],
            changes=_make_serializable(dict(changes)) if changes else {},
            metadata=_make_serializable(dict(metadata)) if metadata else {},
            ip_address=_ip_from_request(request),
            user_agent=_user_agent(request),
            request_path=((request.path if request is not None else '') or '')[:500],
            request_method=((request.method if request is not None else '') or '')[:10],
        )
    except Exception:
        # Never let audit logging break the request
        logger.exception('Failed to write ActivityLog entry for action=%s', action)
