"""
Render a redacted derivative MP4 from a Video's stored Redactions.

Strategy
────────
Non-destructive: we keep the original on disk and produce a new file with
all redactions baked in via ffmpeg's filter_complex graph.

Visual redactions:
  - black_box       → drawbox(t=fill, color=black)
  - blur            → boxblur over the bbox
  - pixelate        → strong boxblur (v1; true mosaic is more complex)

Audio redactions (mute/voice/audio_range):
  - mute / beep     → volume=0 during the range
  - beep            → ALSO mix in a 1 kHz sine wave windowed to the range

Severity → blur strength:
  light/medium/heavy → boxblur=8/16/28

Returns the rendered file's relative path under MEDIA_ROOT, plus stats.
"""

import logging
import os
import subprocess
import time
import uuid
from typing import Iterable

from django.conf import settings

logger = logging.getLogger(__name__)


def _blur_strength(sev: str) -> int:
    return {'light': 8, 'medium': 16, 'heavy': 28}.get(sev, 16)


def _enable_expr(time_start: float, time_end: float) -> str:
    """ffmpeg enable expression for a single time range."""
    return f"between(t,{time_start:.3f},{time_end:.3f})"


def _build_video_filter_chain(redactions, video_w: int, video_h: int) -> str:
    """
    Build the video-side filter chain (everything that operates on [0:v]).
    Returns a single chain string like:
        drawbox=...:enable='...',boxblur=...:enable='...',...
    """
    parts = []
    for r in redactions:
        if not _is_visual(r):
            continue
        if r.bbox_w <= 0 or r.bbox_h <= 0:
            continue
        # Convert 0-1 percentages to pixel coordinates at the original resolution
        x = int(r.bbox_x * video_w)
        y = int(r.bbox_y * video_h)
        w = max(1, int(r.bbox_w * video_w))
        h = max(1, int(r.bbox_h * video_h))
        # Clamp to frame bounds
        if x < 0: x = 0
        if y < 0: y = 0
        if x + w > video_w: w = max(1, video_w - x)
        if y + h > video_h: h = max(1, video_h - y)

        enable = _enable_expr(r.time_start_s, r.time_end_s)

        # All visual redactions render as solid black box (the only reliable
        # option in v1). Blur and pixelate methods are deprecated but if any
        # legacy rows exist, treat them as black box too.
        parts.append(
            f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill:enable='{enable}'"
        )
    if not parts:
        # No visual redactions — pass-through
        return "null"
    return ",".join(parts)


def _build_audio_mute_expr(redactions) -> str:
    """
    Return an enable expression that is TRUE during any audio mute/beep range.
    Used as `volume=0:enable='...'` to silence those ranges.
    Returns '0' (always-false → no muting) if there are no audio redactions.
    """
    parts = []
    for r in redactions:
        if not _is_audio(r):
            continue
        if r.method in ('mute', 'beep'):
            parts.append(_enable_expr(r.time_start_s, r.time_end_s))
    if not parts:
        return '0'
    # `+` acts as logical OR for numeric enable values
    return '+'.join(parts)


def _build_beep_enable_expr(redactions) -> str:
    """Enable expression that is TRUE only during 'beep' ranges (for sine overlay)."""
    parts = []
    for r in redactions:
        if not _is_audio(r):
            continue
        if r.method == 'beep':
            parts.append(_enable_expr(r.time_start_s, r.time_end_s))
    if not parts:
        return '0'
    return '+'.join(parts)


def _has_any_beep(redactions) -> bool:
    return any(_is_audio(r) and r.method == 'beep' for r in redactions)


def _is_visual(r) -> bool:
    return r.target_type in ('face', 'region', 'text')


def _is_audio(r) -> bool:
    return r.target_type in ('voice', 'audio_range')


def render_redacted_video(
    *, src_path: str, dst_path: str,
    redactions: Iterable,
    video_w: int, video_h: int,
    duration_s: float,
) -> tuple[bool, str]:
    """
    Run ffmpeg to produce a redacted MP4 at dst_path.
    Returns (success, message).
    """
    redactions = list(redactions)

    video_chain = _build_video_filter_chain(redactions, video_w, video_h)
    audio_mute_expr = _build_audio_mute_expr(redactions)
    has_beep = _has_any_beep(redactions)
    beep_enable_expr = _build_beep_enable_expr(redactions)

    # Build the input args
    args = [
        settings.FFMPEG_PATH,
        '-y',                          # overwrite output
        '-loglevel', 'warning',
        '-i', src_path,                # [0]
    ]
    if has_beep:
        # Generate a 1kHz sine for the full duration; we'll window via volume= enable
        args += ['-f', 'lavfi', '-t', f'{duration_s:.3f}',
                 '-i', 'sine=frequency=1000:sample_rate=44100']

    # Build filter_complex
    # Video side: [0:v] → chain → [vout]
    # Audio side: [0:a] → volume=0:enable=(mute_expr) → [aorig]
    #             if beep: [1:a] → volume=0.18:enable=(beep_expr) → [abeep]
    #                      [aorig][abeep] amix=inputs=2:duration=first → [aout]
    fc_parts = []
    if video_chain and video_chain != 'null':
        fc_parts.append(f"[0:v]{video_chain}[vout]")
        v_map = '[vout]'
    else:
        v_map = '0:v'        # stream specifier, no brackets

    # Audio chain
    # IMPORTANT: ffmpeg's -map argument distinguishes
    #   '[label]'  → labelled filter graph output
    #   '0:a'      → stream specifier (raw audio of input 0)
    # If we never push audio through filter_complex, map it as '0:a' (no brackets).
    a_chain_pieces = []
    if audio_mute_expr != '0':
        a_chain_pieces.append(f"[0:a]volume=0:enable='{audio_mute_expr}'[aorig]")
        a_main_label = '[aorig]'
    else:
        a_main_label = None   # no audio filtering needed

    if has_beep:
        # Generate beep only inside beep ranges by silencing the sine elsewhere
        a_chain_pieces.append(
            f"[1:a]volume=0.18:enable='{beep_enable_expr}',"
            f"volume=0:enable='not({beep_enable_expr})'[abeep]"
        )
        if a_main_label:
            a_chain_pieces.append(f"{a_main_label}[abeep]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        else:
            a_chain_pieces.append(f"[0:a][abeep]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        a_map = '[aout]'
    elif a_main_label:
        a_map = a_main_label
    else:
        a_map = '0:a?'        # '?' = optional, in case the video has no audio track

    fc_parts.extend(a_chain_pieces)

    if fc_parts:
        args += ['-filter_complex', ';'.join(fc_parts)]

    args += [
        '-map', v_map,
        '-map', a_map,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
        '-c:a', 'aac',     '-b:a', '160k',
        '-movflags', '+faststart',
        dst_path,
    ]

    logger.info('render_redacted_video: ffmpeg %s', ' '.join(repr(a) for a in args))
    try:
        result = subprocess.run(args, capture_output=True, timeout=3600)
        if result.returncode != 0:
            tail = result.stderr[-1500:].decode('utf-8', errors='replace')
            return False, f'ffmpeg exited {result.returncode}: {tail}'
        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            return False, 'ffmpeg finished but output file is empty'
        return True, 'ok'
    except subprocess.TimeoutExpired:
        return False, 'ffmpeg timed out after 1 hour'
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def new_render_filename(video_id) -> str:
    """Generate a unique filename for the rendered output."""
    ts = int(time.time())
    short = uuid.uuid4().hex[:8]
    return f'redacted/{video_id}__{ts}_{short}.mp4'
