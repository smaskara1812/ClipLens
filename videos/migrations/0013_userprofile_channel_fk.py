"""
Migration 0013: Two structural changes in one migration
  1. Add UserProfile model (role field per user)
  2. Change Channel.owner from OneToOneField → ForeignKey
     (allows editors to own multiple channels)
  3. Data migration: seed UserProfile for every existing user
     - is_superuser → superadmin
     - has a channel   → editor
     - otherwise       → viewer
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def seed_profiles(apps, schema_editor):
    User        = apps.get_model('auth', 'User')
    UserProfile = apps.get_model('videos', 'UserProfile')
    Channel     = apps.get_model('videos', 'Channel')

    # Build set of user PKs that own at least one channel (after FK change)
    owner_ids = set(Channel.objects.exclude(owner_id=None).values_list('owner_id', flat=True))

    for user in User.objects.all():
        if user.is_superuser:
            role = 'superadmin'
        elif user.pk in owner_ids:
            role = 'editor'
        else:
            role = 'viewer'
        UserProfile.objects.get_or_create(user=user, defaults={'role': role})


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0012_detectedface_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── 1. Add UserProfile ─────────────────────────────────────────────
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(
                    choices=[
                        ('superadmin', 'Super Admin'),
                        ('editor',     'Editor'),
                        ('viewer',     'Viewer'),
                    ],
                    default='viewer',
                    max_length=20,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='profile',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),

        # ── 2. Change Channel.owner OneToOneField → ForeignKey ─────────────
        migrations.AlterField(
            model_name='channel',
            name='owner',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='channels',
                to=settings.AUTH_USER_MODEL,
            ),
        ),

        # ── 3. Seed UserProfile rows for existing users ────────────────────
        migrations.RunPython(seed_profiles, migrations.RunPython.noop),
    ]
