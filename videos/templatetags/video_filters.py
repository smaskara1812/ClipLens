import json as _json
from django import template

register = template.Library()


def _fmt_seconds(seconds):
    """Convert a duration in seconds to H:MM:SS or M:SS string."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return '—'
    if total < 0:
        return '—'
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


@register.filter
def duration_fmt(seconds):
    return _fmt_seconds(seconds)


@register.filter
def format_duration(seconds):
    return _fmt_seconds(seconds)


@register.filter
def split(value, delimiter=','):
    if value is None:
        return []
    return str(value).split(delimiter)


@register.filter
def tojson(value):
    """Serialize a Python value to a safe JSON string (for use inside JS)."""
    return _json.dumps(value)


@register.filter
def get_item(dictionary, key):
    """Dict lookup: {{ my_dict|get_item:key }}"""
    return dictionary.get(key, '')
