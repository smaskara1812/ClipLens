from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0019_add_photo_model'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Photo: duplicate detection fields ────────────────────────────────
        migrations.AddField(
            model_name='photo',
            name='is_potential_duplicate',
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name='photo',
            name='duplicate_of',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='duplicates',
                to='videos.photo',
            ),
        ),

        # ── Album ─────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='Album',
            fields=[
                ('id',          models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)),
                ('title',       models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('is_smart',    models.BooleanField(default=False)),
                ('smart_filter',models.JSONField(null=True, blank=True)),
                ('is_public',   models.BooleanField(default=True)),
                ('share_token', models.CharField(max_length=64, unique=True, blank=True)),
                ('photo_count', models.PositiveIntegerField(default=0)),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
                ('updated_at',  models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='albums',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('channel', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='albums',
                    to='videos.channel',
                )),
                ('cover_photo', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to='videos.photo',
                )),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='album',
            index=models.Index(fields=['owner', 'is_smart'], name='album_owner_smart'),
        ),
        migrations.AddIndex(
            model_name='album',
            index=models.Index(fields=['share_token'], name='album_share_token'),
        ),

        # ── AlbumPhoto (through model) ─────────────────────────────────────
        migrations.CreateModel(
            name='AlbumPhoto',
            fields=[
                ('id',       models.AutoField(primary_key=True)),
                ('order',    models.PositiveIntegerField(default=0)),
                ('added_at', models.DateTimeField(auto_now_add=True)),
                ('album', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='album_photos',
                    to='videos.album',
                )),
                ('photo', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='album_photos',
                    to='videos.photo',
                )),
            ],
            options={
                'ordering': ['order', 'added_at'],
            },
        ),
        migrations.AlterUniqueTogether(
            name='albumphoto',
            unique_together={('album', 'photo')},
        ),
        migrations.AddIndex(
            model_name='albumphoto',
            index=models.Index(fields=['album', 'order'], name='albphoto_album_order'),
        ),
    ]
