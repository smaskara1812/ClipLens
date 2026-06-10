"""
Phase 6 — Library-wide RedactionPolicy engine.

A RedactionPolicy says "blur Riya in every video, including future uploads."
This module turns a policy into concrete Redaction rows on matching videos,
runs the metadata scrub, and queues renders. It also handles revoking
policies and restoring originals.

Public functions:
  apply_policy_to_existing(policy)
      → Walks every video, finds matches, creates saved Redactions,
        runs scrub, queues renders. Returns counts.

  apply_policy_to_video(policy, video)
      → Same as above but for one video. Used by the post-save signal
        when a new video finishes processing.

  revoke_policy(policy, performed_by_username)
      → Marks policy revoked, deletes auto-created Redactions across
        every video, restores transcripts (unless true_erasure), and
        queues fresh renders so the videos return to a pre-policy state.
"""

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Match → create Redactions for one video ──────────────────────────────────

def _create_face_redactions_for_video(policy, video, padding_pct=0.50):
    """Walk DetectedFace rows for the policy's identity in this video,
    create one saved Redaction per detection, link to policy."""
    from .models import Redaction, DetectedFace
    import json as _json

    if not policy.target_face_identity_id:
        return []
    db_alias = video._state.db or 'default'
    detections = list(
        DetectedFace.objects.using(db_alias)
        .filter(video=video, identity_id=policy.target_face_identity_id)
        .order_by('timestamp')
        .values('timestamp', 'bbox')
    )
    if not detections:
        return []

    try:
        vw, vh = video.resolution.split('x', 1)
        vw, vh = max(1, int(vw)), max(1, int(vh))
    except Exception:
        vw, vh = 1920, 1080

    # Hold between samples
    from django.conf import settings
    hold = float(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5)) + 0.5

    to_create = []
    now = timezone.now()
    for i, d in enumerate(detections):
        try:
            box = _json.loads(d['bbox']) if isinstance(d['bbox'], str) else d['bbox']
            x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        except Exception:
            continue
        w = max(0, x2 - x1)
        h = max(0, y2 - y1)
        pad_x, pad_y = w * padding_pct, h * padding_pct
        x1 = max(0, x1 - pad_x); x2 = min(vw, x2 + pad_x)
        y1 = max(0, y1 - pad_y); y2 = min(vh, y2 + pad_y)
        bw = (x2 - x1) / vw
        bh = (y2 - y1) / vh
        if bw <= 0 or bh <= 0:
            continue
        t_start = max(0, float(d['timestamp']) - 0.2)
        if i + 1 < len(detections):
            t_end = min(float(detections[i + 1]['timestamp']) + 0.2,
                        float(d['timestamp']) + hold)
        else:
            t_end = float(d['timestamp']) + hold
        to_create.append(Redaction(
            video=video,
            target_type=Redaction.TARGET_FACE,
            target_face_identity_id=policy.target_face_identity_id,
            method=policy.visual_method or 'black_box',
            severity=policy.visual_severity or 'heavy',
            time_start_s=t_start,
            time_end_s=t_end,
            spatial_mode=Redaction.SPATIAL_TRACKED_FACE,
            bbox_x=x1 / vw, bbox_y=y1 / vh, bbox_w=bw, bbox_h=bh,
            source=Redaction.SOURCE_POLICY,
            label=f'{policy.target_label} (policy)',
            applied_by_policy_id=policy.pk,
            is_saved=True,
            saved_at=now,
            created_by_username=policy.created_by_username or '',
        ))
    if to_create:
        created = Redaction.objects.using(db_alias).bulk_create(to_create)
        logger.info('policy %s: created %d face redactions on video %s',
                    policy.pk, len(created), video.pk)
        return list(Redaction.objects.using(db_alias).filter(
            applied_by_policy_id=policy.pk, video=video))
    return []


def _create_voice_redactions_for_video(policy, video):
    """Walk VideoSegment rows for the speaker, create merged Redactions."""
    from .models import Redaction, VideoSegment

    if not policy.target_speaker_identity_id:
        return []
    if not policy.audio_method:
        return []      # policy targets voice but no audio method set
    db_alias = video._state.db or 'default'
    segments = list(
        VideoSegment.objects.using(db_alias)
        .filter(video=video, speaker_identity_id=policy.target_speaker_identity_id)
        .order_by('start_seconds')
        .values('start_seconds', 'end_seconds')
    )
    if not segments:
        return []

    # Merge adjacent (gap < 0.5s) for cleaner playback
    merged = []
    cur_s = segments[0]['start_seconds']
    cur_e = segments[0]['end_seconds']
    for s in segments[1:]:
        if s['start_seconds'] - cur_e <= 0.5:
            cur_e = max(cur_e, s['end_seconds'])
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s['start_seconds'], s['end_seconds']
    merged.append((cur_s, cur_e))

    now = timezone.now()
    to_create = [
        Redaction(
            video=video,
            target_type=Redaction.TARGET_VOICE,
            target_speaker_identity_id=policy.target_speaker_identity_id,
            method=policy.audio_method,
            severity='medium',
            time_start_s=max(0, s),
            time_end_s=e,
            spatial_mode=Redaction.SPATIAL_WHOLE_FRAME,
            source=Redaction.SOURCE_POLICY,
            label=f'{policy.target_label} (policy)',
            applied_by_policy_id=policy.pk,
            is_saved=True,
            saved_at=now,
            created_by_username=policy.created_by_username or '',
        )
        for (s, e) in merged
    ]
    if to_create:
        Redaction.objects.using(db_alias).bulk_create(to_create)
        logger.info('policy %s: created %d voice redactions on video %s',
                    policy.pk, len(to_create), video.pk)
        return list(Redaction.objects.using(db_alias).filter(
            applied_by_policy_id=policy.pk, video=video))
    return []


# ── Public: apply ────────────────────────────────────────────────────────────

def _create_face_redactions_for_photo(policy, photo, padding_pct=0.20):
    """
    Photo equivalent of _create_face_redactions_for_video.
    One Redaction per DetectedFace of the policy's identity in the photo.
    Photos have no time dimension so we just store bbox.
    """
    from .models import Redaction, DetectedFace
    import json as _json

    if not policy.target_face_identity_id:
        return []
    db_alias = photo._state.db or 'default'
    detections = list(
        DetectedFace.objects.using(db_alias)
        .filter(photo=photo, identity_id=policy.target_face_identity_id)
        .values('bbox')
    )
    if not detections:
        return []

    pw, ph = max(1, photo.width or 0), max(1, photo.height or 0)
    if pw <= 1 or ph <= 1:
        # Fall back to whole-frame if we don't know dimensions yet
        from PIL import Image as _PI
        try:
            with _PI.open(photo.file.path) as im:
                pw, ph = im.size
        except Exception:
            pw, ph = 1, 1

    to_create = []
    now = timezone.now()
    for d in detections:
        try:
            box = _json.loads(d['bbox']) if isinstance(d['bbox'], str) else d['bbox']
            x1, y1, x2, y2 = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        except Exception:
            continue
        w = max(0, x2 - x1)
        h = max(0, y2 - y1)
        pad_x, pad_y = w * padding_pct, h * padding_pct
        x1 = max(0, x1 - pad_x); x2 = min(pw, x2 + pad_x)
        y1 = max(0, y1 - pad_y); y2 = min(ph, y2 + pad_y)
        bw = (x2 - x1) / pw
        bh = (y2 - y1) / ph
        if bw <= 0 or bh <= 0:
            continue
        to_create.append(Redaction(
            photo=photo,
            target_type=Redaction.TARGET_FACE,
            target_face_identity_id=policy.target_face_identity_id,
            method=policy.visual_method or 'black_box',
            severity=policy.visual_severity or 'heavy',
            time_start_s=0, time_end_s=0,   # N/A for photos
            spatial_mode=Redaction.SPATIAL_FIXED_BOX,
            bbox_x=x1 / pw, bbox_y=y1 / ph, bbox_w=bw, bbox_h=bh,
            source=Redaction.SOURCE_POLICY,
            label=f'{policy.target_label} (policy)',
            applied_by_policy_id=policy.pk,
            is_saved=True,
            saved_at=now,
            created_by_username=policy.created_by_username or '',
        ))
    if to_create:
        created = Redaction.objects.using(db_alias).bulk_create(to_create)
        logger.info('policy %s: created %d face redactions on photo %s',
                    policy.pk, len(created), photo.pk)
        return list(Redaction.objects.using(db_alias).filter(
            applied_by_policy_id=policy.pk, photo=photo))
    return []


def apply_policy_to_photo(policy, photo, *, tenant_slug=''):
    """
    Apply one policy to one photo. Photos only support face-target redaction
    (no audio/transcript dimension). Idempotent — re-applying is a no-op.
    """
    from .models import Redaction
    db_alias = photo._state.db or 'default'

    if policy.target_type != 'face':
        return {'skipped': True, 'reason': 'photo_supports_face_only'}

    existing = Redaction.objects.using(db_alias).filter(
        photo=photo, applied_by_policy_id=policy.pk).exists()
    if existing:
        return {'skipped': True, 'reason': 'already_applied'}

    rdx = _create_face_redactions_for_photo(policy, photo)
    return {
        'created': len(rdx),
        'reason': 'no_matches' if not rdx else 'applied',
    }


def apply_active_policies_to_photo(photo, *, tenant_slug=''):
    """
    Called after analyze_photo_task finishes. Apply every active face policy
    whose target identity appears in this photo.
    """
    from .models import RedactionPolicy, DetectedFace
    db_alias = photo._state.db or 'default'

    identity_ids = set(
        DetectedFace.objects.using(db_alias)
        .filter(photo=photo, identity__isnull=False)
        .values_list('identity_id', flat=True)
    )
    if not identity_ids:
        return {'applied': 0}

    policies = RedactionPolicy.objects.using(db_alias).filter(
        active=True,
        apply_to_future=True,
        target_type='face',
        target_face_identity_id__in=identity_ids,
    )
    total = 0
    for p in policies:
        try:
            res = apply_policy_to_photo(p, photo, tenant_slug=tenant_slug)
            if res.get('created'):
                total += res['created']
        except Exception:
            logger.exception('apply_active_policies_to_photo: policy %s failed', p.pk)
    return {'applied': total, 'policies_considered': policies.count()}


def apply_policy_to_video(policy, video, *, tenant_slug=''):
    """
    Apply one policy to one video. Creates saved Redactions, runs scrub,
    queues a render. Returns dict of counts.
    """
    from .redaction_scrub import scrub_for_redactions
    from .models import Redaction, RedactionRender
    from .redaction_render import new_render_filename
    db_alias = video._state.db or 'default'

    # Skip if this policy already applied to this video (idempotent)
    existing = Redaction.objects.using(db_alias).filter(
        video=video, applied_by_policy_id=policy.pk).exists()
    if existing:
        logger.info('policy %s already applied to video %s; skip', policy.pk, video.pk)
        return {'skipped': True, 'reason': 'already_applied'}

    if policy.target_type == 'face':
        rdx = _create_face_redactions_for_video(policy, video)
    elif policy.target_type == 'voice':
        rdx = _create_voice_redactions_for_video(policy, video)
    else:
        rdx = []

    if not rdx:
        return {'created': 0, 'reason': 'no_matches'}

    # Scrub metadata (if policy requests transcript scrub)
    stats = {}
    if policy.redact_transcripts or policy.target_type == 'voice':
        stats = scrub_for_redactions(video, rdx)
        # True erasure: also wipe redacted_original_text so it's unrecoverable
        if policy.true_erasure:
            from .models import VideoSegment
            VideoSegment.objects.using(db_alias).filter(
                video=video, text='[redacted]',
            ).update(redacted_original_text='')

    # Queue a render (delete prior renders to keep only the latest)
    import os as _os
    storage = video.original_file.storage if video.original_file else None
    prior = list(RedactionRender.objects.using(db_alias).filter(video=video))
    for p in prior:
        if storage and p.file_path:
            try:
                ap = storage.path(p.file_path)
                if _os.path.exists(ap):
                    _os.remove(ap)
            except Exception:
                pass
        p.delete(using=db_alias)
    rel_path = new_render_filename(video.id)
    render = RedactionRender.objects.using(db_alias).create(
        video=video,
        status=RedactionRender.STATUS_QUEUED,
        file_path=rel_path,
        rendered_by_username=f'policy:{policy.pk}',
    )
    try:
        from .tasks import render_redacted_video_task, regenerate_subtitles_after_redaction_task
        render_redacted_video_task.delay(render.id, tenant_slug=tenant_slug or '')
        regenerate_subtitles_after_redaction_task.apply_async(
            args=[str(video.id)], kwargs={'tenant_slug': tenant_slug or ''},
            queue='captions',
        )
    except Exception:
        logger.exception('policy %s: could not queue render for video %s', policy.pk, video.pk)

    return {
        'created': len(rdx),
        'scrub': stats,
        'render_id': render.id,
    }


def apply_policy_to_existing(policy, *, tenant_slug=''):
    """
    Walk every video AND photo in this tenant and apply the policy where
    matches exist. Updates policy stats. Returns aggregate summary.
    """
    from .models import Video, Photo, DetectedFace, VideoSegment
    db_alias = policy._state.db or 'default'

    # Find candidate videos
    if policy.target_type == 'face' and policy.target_face_identity_id:
        video_ids = list(DetectedFace.objects.using(db_alias)
                         .filter(identity_id=policy.target_face_identity_id,
                                 video__isnull=False)
                         .values_list('video_id', flat=True).distinct())
        photo_ids = list(DetectedFace.objects.using(db_alias)
                         .filter(identity_id=policy.target_face_identity_id,
                                 photo__isnull=False)
                         .values_list('photo_id', flat=True).distinct())
    elif policy.target_type == 'voice' and policy.target_speaker_identity_id:
        video_ids = list(VideoSegment.objects.using(db_alias)
                         .filter(speaker_identity_id=policy.target_speaker_identity_id)
                         .values_list('video_id', flat=True).distinct())
        photo_ids = []   # voice policies don't apply to photos
    else:
        video_ids = []
        photo_ids = []

    videos = list(Video.objects.using(db_alias).filter(id__in=video_ids))
    photos = list(Photo.objects.using(db_alias).filter(id__in=photo_ids))
    total_created = 0
    affected_videos = 0
    affected_photos = 0
    for v in videos:
        res = apply_policy_to_video(policy, v, tenant_slug=tenant_slug)
        if res.get('created'):
            total_created += res['created']
            affected_videos += 1
    for p in photos:
        res = apply_policy_to_photo(policy, p, tenant_slug=tenant_slug)
        if res.get('created'):
            total_created += res['created']
            affected_photos += 1

    policy.last_applied_at = timezone.now()
    policy.affected_videos_count = (policy.affected_videos_count or 0) + affected_videos + affected_photos
    policy.auto_created_redactions = (policy.auto_created_redactions or 0) + total_created
    policy.save(using=db_alias, update_fields=[
        'last_applied_at', 'affected_videos_count', 'auto_created_redactions',
    ])
    return {
        'videos_affected': affected_videos,
        'photos_affected': affected_photos,
        'redactions_created': total_created,
        'candidate_videos': len(videos),
        'candidate_photos': len(photos),
    }


# ── Public: revoke ───────────────────────────────────────────────────────────

def revoke_policy(policy, *, performed_by_username='', tenant_slug=''):
    """
    Revoke a policy: delete all auto-created Redactions, restore transcripts
    (unless true_erasure), regenerate subtitles, queue fresh renders.
    """
    from .models import Redaction, RedactionRender, VideoSegment, Video
    from .redaction_render import new_render_filename
    db_alias = policy._state.db or 'default'

    if not policy.active:
        return {'skipped': True, 'reason': 'already_revoked'}

    rdx_qs = Redaction.objects.using(db_alias).filter(applied_by_policy_id=policy.pk)
    affected_video_ids = list(rdx_qs.values_list('video_id', flat=True).distinct())

    # Restore transcripts where possible (unless true_erasure)
    restored_segments = 0
    if not policy.true_erasure:
        # Find any segment touched by this policy's voice redactions whose
        # text == [redacted] and has a stashed original — put it back
        from django.db.models import F, Q
        seg_qs = (VideoSegment.objects.using(db_alias)
                  .filter(video_id__in=affected_video_ids, text='[redacted]')
                  .exclude(redacted_original_text=''))
        for seg in seg_qs:
            seg.text = seg.redacted_original_text
            seg.redacted_original_text = ''
            seg.save(using=db_alias, update_fields=['text', 'redacted_original_text'])
            restored_segments += 1

    # Hard-delete the auto-created Redactions
    deleted_redactions = rdx_qs.delete()[0]

    # Mark policy revoked
    policy.active = False
    policy.revoked_at = timezone.now()
    policy.revoked_by_username = performed_by_username or ''
    policy.save(using=db_alias, update_fields=['active', 'revoked_at', 'revoked_by_username'])

    # Queue fresh renders for the affected videos so the visual redactions
    # also disappear from the rendered output.
    import os as _os
    from .tasks import render_redacted_video_task, regenerate_subtitles_after_redaction_task
    re_rendered = 0
    for vid_id in affected_video_ids:
        try:
            v = Video.objects.using(db_alias).get(id=vid_id)
        except Video.DoesNotExist:
            continue
        # Delete old renders
        storage = v.original_file.storage if v.original_file else None
        for p in list(RedactionRender.objects.using(db_alias).filter(video=v)):
            if storage and p.file_path:
                try:
                    ap = storage.path(p.file_path)
                    if _os.path.exists(ap):
                        _os.remove(ap)
                except Exception:
                    pass
            p.delete(using=db_alias)
        # Only queue a fresh render if other (non-policy) redactions still exist
        still = Redaction.objects.using(db_alias).filter(video=v).count()
        if still:
            rel_path = new_render_filename(v.id)
            render = RedactionRender.objects.using(db_alias).create(
                video=v,
                status=RedactionRender.STATUS_QUEUED,
                file_path=rel_path,
                rendered_by_username=f'revoke-policy:{policy.pk}',
            )
            try:
                render_redacted_video_task.delay(render.id, tenant_slug=tenant_slug or '')
            except Exception:
                logger.exception('revoke_policy: could not queue render for %s', v.pk)
        # Always regenerate subtitles so transcripts come back
        try:
            regenerate_subtitles_after_redaction_task.apply_async(
                args=[str(v.id)], kwargs={'tenant_slug': tenant_slug or ''},
                queue='captions',
            )
        except Exception:
            pass
        re_rendered += 1

    return {
        'videos_affected': len(affected_video_ids),
        'redactions_deleted': deleted_redactions,
        'segments_restored': restored_segments,
        're_rendered': re_rendered,
    }
