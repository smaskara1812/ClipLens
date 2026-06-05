"""
Phase 5 — surgical metadata scrub on save.

When a redaction is saved (is_saved=True), we *also* remove the matching
metadata from the indexed AI data so the redacted person/voice/region:
  • stops appearing in face / speaker search results
  • their words disappear from full-text transcript search
  • VTT subtitle files in every language are rewritten with "[redacted]"

This is the "Option B — surgical metadata removal" path the user chose.
For library-wide policies (Phase 6), the same primitives are reused but
applied across every video.

Four target paths:
  - face         (with target_face_identity)    → delete DetectedFace rows
  - voice        (with target_speaker_identity) → edit VideoSegment text + VTTs
  - region       (manual visual bbox)           → spatial+temporal overlap → DetectedFace
  - audio_range  (manual time-range)            → temporal overlap → VideoSegment + VTTs

All operations are idempotent and best-effort — failures are logged but
never raise into the save flow.
"""

import logging
import os
import re
from typing import Iterable

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

REDACTED_TOKEN = '[redacted]'


# ── Bbox overlap (IoU > threshold) ────────────────────────────────────────────

def _iou(box_a, box_b):
    """IoU between two boxes in 0-1 percentages: each is (x, y, w, h)."""
    ax1, ay1, aw, ah = box_a
    bx1, by1, bw, bh = box_b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# ── VTT helpers ───────────────────────────────────────────────────────────────

_VTT_TS = re.compile(
    r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\.(\d{3})\s+-->\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\.(\d{3})'
)


def _parse_vtt_timestamp(line: str):
    """Returns (start_s, end_s) or None if the line isn't a VTT timestamp."""
    m = _VTT_TS.match(line.strip())
    if not m:
        return None
    g = m.groups()
    # Handle both HH:MM:SS.mmm and MM:SS.mmm
    if g[2] is not None:
        h1, m1, s1, ms1 = int(g[0]), int(g[1]), int(g[2]), int(g[3])
    else:
        h1, m1, s1, ms1 = 0, int(g[0]), int(g[1]), int(g[3])
    if g[6] is not None:
        h2, m2, s2, ms2 = int(g[4]), int(g[5]), int(g[6]), int(g[7])
    else:
        h2, m2, s2, ms2 = 0, int(g[4]), int(g[5]), int(g[7])
    start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
    end   = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
    return (start, end)


def _ranges_overlap(a_start, a_end, ranges):
    """True if (a_start, a_end) overlaps any (s, e) in ranges."""
    for s, e in ranges:
        if a_start < e and a_end > s:
            return True
    return False


def regenerate_vtt_for_video(video, redacted_ranges):
    """
    Rewrite every Subtitle file for this video, replacing cue text with
    "[redacted]" for any cue whose timestamp range overlaps a redacted range.

    redacted_ranges: iterable of (start_s, end_s) tuples.
    """
    from .models import Subtitle
    if not redacted_ranges:
        return 0
    db_alias = video._state.db or 'default'
    subs = list(Subtitle.objects.using(db_alias).filter(video=video))
    rewritten = 0
    for sub in subs:
        try:
            path = sub.file.path
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
            # We DROP entire cues whose timestamp range overlaps any redacted
            # range, instead of writing "[redacted]" text — the viewer sees
            # no caption at all during muted/beeped ranges.
            out_lines = []
            i = 0
            while i < len(lines):
                line = lines[i]
                ts = _parse_vtt_timestamp(line)
                if ts is None:
                    out_lines.append(line)
                    i += 1
                    continue
                # Found a cue header. The line just BEFORE may be the cue id (a
                # bare number / identifier). Look back to handle that block too.
                drop_cue = _ranges_overlap(ts[0], ts[1], redacted_ranges)
                if drop_cue and out_lines:
                    # If the previous line is a cue identifier (non-empty,
                    # non-blank, no `-->`), drop it too.
                    prev = out_lines[-1].strip()
                    if prev and '-->' not in prev and prev != 'WEBVTT':
                        out_lines.pop()
                # Skip the timestamp line if dropping; otherwise keep it
                if not drop_cue:
                    out_lines.append(line)
                i += 1
                # Consume cue text lines
                while i < len(lines) and lines[i].strip():
                    if not drop_cue:
                        out_lines.append(lines[i])
                    i += 1
                # Consume the blank separator after the cue
                if i < len(lines) and not drop_cue:
                    out_lines.append(lines[i])
                i += 1
            with open(path, 'w', encoding='utf-8') as fh:
                fh.writelines(out_lines)
            rewritten += 1
        except Exception:
            logger.exception('VTT regeneration failed for subtitle %s', sub.pk)
    return rewritten


# ── Scrub by target ───────────────────────────────────────────────────────────

def _scrub_face_identity(video, identity):
    """Delete every DetectedFace row for this identity in this video."""
    from .models import DetectedFace
    db_alias = video._state.db or 'default'
    n = DetectedFace.objects.using(db_alias).filter(video=video, identity=identity).delete()[0]
    logger.info('scrub face: video=%s identity=%s deleted=%d db=%s',
                video.pk, identity.pk, n, db_alias)
    return {'detected_faces_deleted': n}


def _scrub_speaker_identity(video, speaker, *, audio_ranges):
    """
    Replace VideoSegment.text with [redacted] for every segment of this speaker.
    Original text preserved in redacted_original_text.
    Regenerate VTT files in every language.
    """
    from .models import VideoSegment
    db_alias = video._state.db or 'default'
    qs = VideoSegment.objects.using(db_alias).filter(video=video, speaker_identity=speaker)
    segs = list(qs)
    redacted_ranges = []
    redacted_count = 0
    for seg in segs:
        if seg.text == REDACTED_TOKEN:
            redacted_ranges.append((seg.start_seconds, seg.end_seconds))
            continue
        if not seg.redacted_original_text:
            seg.redacted_original_text = seg.text
        seg.text = REDACTED_TOKEN
        seg.save(using=db_alias, update_fields=['text', 'redacted_original_text'])
        redacted_ranges.append((seg.start_seconds, seg.end_seconds))
        redacted_count += 1
    vtt_n = regenerate_vtt_for_video(video, audio_ranges + redacted_ranges)
    logger.info('scrub speaker: video=%s speaker=%s segs=%d vtts=%d db=%s',
                video.pk, speaker.pk, redacted_count, vtt_n, db_alias)
    return {'segments_redacted': redacted_count, 'vtts_rewritten': vtt_n}


def _scrub_manual_region(video, redaction, *, iou_threshold=0.30):
    """
    For a manual visual region, find DetectedFace rows whose bbox spatially
    overlaps (IoU > threshold) AND whose timestamp falls within the
    redaction's time range. Delete those rows.
    """
    from .models import DetectedFace
    import json as _json
    db_alias = video._state.db or 'default'
    try:
        vw, vh = video.resolution.split('x', 1)
        vw, vh = max(1, int(vw)), max(1, int(vh))
    except Exception:
        vw, vh = 1920, 1080
    target_box = (redaction.bbox_x, redaction.bbox_y,
                  redaction.bbox_w, redaction.bbox_h)
    qs = DetectedFace.objects.using(db_alias).filter(
        video=video,
        timestamp__gte=redaction.time_start_s,
        timestamp__lte=redaction.time_end_s,
    )
    to_delete = []
    for det in qs:
        try:
            box = _json.loads(det.bbox) if isinstance(det.bbox, str) else det.bbox
            x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        except Exception:
            continue
        det_box = (x1 / vw, y1 / vh, (x2 - x1) / vw, (y2 - y1) / vh)
        if _iou(target_box, det_box) >= iou_threshold:
            to_delete.append(det.pk)
    deleted = 0
    if to_delete:
        deleted = DetectedFace.objects.using(db_alias).filter(pk__in=to_delete).delete()[0]
    logger.info('scrub manual region: video=%s rdx=%s candidates=%d deleted=%d db=%s',
                video.pk, redaction.pk, qs.count(), deleted, db_alias)
    return {'detected_faces_deleted': deleted}


def _scrub_manual_audio_range(video, redaction):
    """
    For a manual audio range, find VideoSegments that overlap the time
    window. Replace text with [redacted] and regenerate VTTs.
    Returns dict of counts.
    """
    from .models import VideoSegment
    db_alias = video._state.db or 'default'
    qs = VideoSegment.objects.using(db_alias).filter(
        video=video,
        start_seconds__lt=redaction.time_end_s,
        end_seconds__gt=redaction.time_start_s,
    )
    segs = list(qs)
    redacted_count = 0
    for seg in segs:
        if seg.text == REDACTED_TOKEN:
            continue
        if not seg.redacted_original_text:
            seg.redacted_original_text = seg.text
        seg.text = REDACTED_TOKEN
        seg.save(using=db_alias, update_fields=['text', 'redacted_original_text'])
        redacted_count += 1
    vtt_n = regenerate_vtt_for_video(
        video, [(redaction.time_start_s, redaction.time_end_s)]
    )
    logger.info('scrub audio range: video=%s rdx=%s segs=%d vtts=%d db=%s',
                video.pk, redaction.pk, redacted_count, vtt_n, db_alias)
    return {'segments_redacted': redacted_count, 'vtts_rewritten': vtt_n}


# ── Public dispatch ───────────────────────────────────────────────────────────

def scrub_for_redactions(video, redactions: Iterable):
    """
    Apply surgical metadata scrub for every redaction in the iterable.
    Aggregates results into a single dict for the save endpoint response.

    Never raises — logs and skips on per-redaction failure.
    """
    totals = {
        'detected_faces_deleted': 0,
        'segments_redacted':      0,
        'vtts_rewritten':         0,
    }
    # Collect audio ranges to do a single VTT regen pass at the end
    audio_ranges_for_speaker = []
    for r in redactions:
        try:
            if r.target_type == 'face' and r.target_face_identity_id:
                stats = _scrub_face_identity(video, r.target_face_identity)
            elif r.target_type == 'voice' and r.target_speaker_identity_id:
                stats = _scrub_speaker_identity(
                    video, r.target_speaker_identity,
                    audio_ranges=[(r.time_start_s, r.time_end_s)],
                )
            elif r.target_type == 'region':
                stats = _scrub_manual_region(video, r)
            elif r.target_type == 'audio_range':
                stats = _scrub_manual_audio_range(video, r)
            else:
                continue
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
        except Exception:
            logger.exception('scrub_for_redactions: failed on redaction %s', r.pk)
    return totals
