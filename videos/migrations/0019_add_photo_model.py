"""
Migration 0019 — Photo model for Digital Asset Management.

Creates the videos_photo table with:
  - Standard fields (id, title, description, channel, category, tags, file, thumbnail,
    width, height, file_size, labels, face_count, face_names, scene_description,
    clip_embedding, status, visibility, views_count, uploaded_by, timestamps)
  - B-tree indexes matching Video's pattern
  - Postgres FTS GIN indexes on title/description/tags and scene_description/labels
  - pgvector HNSW index on clip_embedding (cosine)
  - pg_trgm GIN indexes on title and labels (fuzzy search)
"""
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0018_add_pg_trgm_fuzzy_indexes'),
    ]

    operations = [
        # ── Create the table ───────────────────────────────────────────────────
        migrations.CreateModel(
            name='Photo',
            fields=[
                ('id',          models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('title',       models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('channel',     models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='photos',
                    to='videos.channel',
                )),
                ('category',    models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='photos',
                    to='videos.category',
                )),
                ('tags',        models.CharField(blank=True, max_length=500, help_text='Comma-separated tags')),
                ('file',        models.ImageField(upload_to='photos/originals/')),
                ('thumbnail',   models.ImageField(blank=True, null=True, upload_to='photos/thumbnails/')),
                ('width',       models.IntegerField(blank=True, null=True)),
                ('height',      models.IntegerField(blank=True, null=True)),
                ('file_size',   models.BigIntegerField(blank=True, null=True)),
                ('labels',            models.TextField(blank=True, help_text='Comma-separated YOLO class labels')),
                ('face_count',        models.IntegerField(default=0)),
                ('face_names',        models.TextField(blank=True, help_text='Comma-separated identified names')),
                ('scene_description', models.TextField(blank=True, help_text='BLIP/Florence-2 scene description')),
                # clip_embedding added as RunSQL below (needs pgvector type)
                ('status',           models.CharField(
                    choices=[('pending','Pending'),('processing','Processing'),('ready','Ready'),('failed','Failed')],
                    default='pending', max_length=20,
                )),
                ('processing_error', models.TextField(blank=True)),
                ('visibility',   models.CharField(
                    choices=[('public','Public'),('private','Private (URL only)')],
                    default='public', max_length=20,
                )),
                ('views_count',  models.PositiveBigIntegerField(default=0)),
                ('uploaded_by',  models.CharField(blank=True, max_length=100)),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
                ('updated_at',   models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),

        # ── Add standard B-tree indexes ────────────────────────────────────────
        migrations.AddIndex(
            model_name='photo',
            index=models.Index(fields=['status', 'visibility'], name='photo_status_vis'),
        ),
        migrations.AddIndex(
            model_name='photo',
            index=models.Index(fields=['channel', 'status', 'visibility'], name='photo_ch_status_vis'),
        ),
        migrations.AddIndex(
            model_name='photo',
            index=models.Index(fields=['created_at'], name='photo_created_at'),
        ),

        # ── Add clip_embedding vector column + HNSW index (Postgres only) ─────
        migrations.RunSQL(
            sql="""
                ALTER TABLE videos_photo
                    ADD COLUMN IF NOT EXISTS clip_embedding vector(512);

                CREATE INDEX IF NOT EXISTS photo_clip_hnsw
                    ON videos_photo
                    USING hnsw (clip_embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS photo_clip_hnsw;
                ALTER TABLE videos_photo DROP COLUMN IF EXISTS clip_embedding;
            """,
            state_operations=[
                migrations.AddField(
                    model_name='photo',
                    name='clip_embedding',
                    field=__import__(
                        'pgvector.django', fromlist=['VectorField']
                    ).VectorField(dimensions=512, null=True, blank=True,
                                  help_text='CLIP image embedding (512-dim float vector)'),
                ),
            ],
        ),

        # ── Postgres FTS GIN indexes ───────────────────────────────────────────
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS photo_title_desc_tags_fts
                    ON videos_photo
                    USING gin (
                        to_tsvector('english',
                            coalesce(title, '') || ' ' ||
                            coalesce(description, '') || ' ' ||
                            coalesce(tags, ''))
                    );

                CREATE INDEX IF NOT EXISTS photo_scene_desc_fts
                    ON videos_photo
                    USING gin (
                        to_tsvector('english', coalesce(scene_description, ''))
                    );

                CREATE INDEX IF NOT EXISTS photo_labels_fts
                    ON videos_photo
                    USING gin (
                        to_tsvector('english', coalesce(labels, ''))
                    );
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS photo_title_desc_tags_fts;
                DROP INDEX IF EXISTS photo_scene_desc_fts;
                DROP INDEX IF EXISTS photo_labels_fts;
            """,
        ),

        # ── pg_trgm GIN indexes for fuzzy search ──────────────────────────────
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS photo_title_trgm
                    ON videos_photo USING gin (title gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS photo_labels_trgm
                    ON videos_photo USING gin (labels gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS photo_scene_desc_trgm
                    ON videos_photo USING gin (scene_description gin_trgm_ops);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS photo_title_trgm;
                DROP INDEX IF EXISTS photo_labels_trgm;
                DROP INDEX IF EXISTS photo_scene_desc_trgm;
            """,
        ),
    ]
