from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'videos'

    def ready(self):
        from django.db.models.signals import post_save
        from django.contrib.auth.models import User
        from django.dispatch import receiver

        @receiver(post_save, sender=User)
        def create_user_profile(sender, instance, created, raw, using, **kwargs):
            """Auto-create a UserProfile (viewer by default) for every new User.

            IMPORTANT: pin the profile to the same DB the user was saved to.
            Without this, the router may send the profile elsewhere and a
            later explicit create in that DB would hit the unique constraint.
            """
            if created and not raw:  # raw=True during loaddata — fixture provides the profile
                from .models import UserProfile
                role = UserProfile.ROLE_SUPERADMIN if instance.is_superuser else UserProfile.ROLE_VIEWER
                UserProfile.objects.using(using).get_or_create(
                    user=instance, defaults={'role': role}
                )

        # Wire authentication audit signals (login / logout / login_failed)
        from . import signals  # noqa: F401

        # ── Subtitle ↔ VideoSegment sync ──────────────────────────────
        # When a primary-language Subtitle (auto-generated, not a translation)
        # is saved, parse its VTT and replace VideoSegment rows so DB stays
        # consistent with what's on disk.
        @receiver(post_save, sender='videos.Subtitle')
        def sync_segments_from_primary_subtitle(sender, instance, created, raw, using, **kwargs):
            if raw:
                return
            if instance.is_translation:
                # Translations are language variants — segments are
                # language-agnostic (one set per video, based on primary).
                return
            if not instance.is_auto_generated:
                # Manual uploads are NOT auto-synced to avoid clobbering
                # user-curated transcripts. They can use the repair tool.
                return
            try:
                from .management.commands.repair_video_segments import parse_vtt_file
                from .models import VideoSegment
                path = instance.file.path
                import os as _os
                if not _os.path.exists(path):
                    return
                cues = list(parse_vtt_file(path))
                if not cues:
                    return
                db_alias = instance._state.db or 'default'
                VideoSegment.objects.using(db_alias).filter(video_id=instance.video_id).delete()
                VideoSegment.objects.using(db_alias).bulk_create([
                    VideoSegment(video_id=instance.video_id,
                                 start_seconds=s, end_seconds=e, text=t)
                    for (s, e, t) in cues
                ])
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    'sync_segments_from_primary_subtitle failed for subtitle %s',
                    getattr(instance, 'pk', '?'))
