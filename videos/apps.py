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
