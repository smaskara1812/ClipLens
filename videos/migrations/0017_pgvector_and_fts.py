"""
Migration 0017 — pgvector + Postgres full-text search indexes

1. Enables the pgvector extension (vector column type + ANN indexes).
2. Converts VideoFrame.clip_embedding from TEXT → vector(512) in-place,
   turning empty strings into NULLs.
3. Adds an HNSW index for fast cosine-similarity ANN search.
4. Adds GIN expression indexes on tsvector columns for full-text search on:
   - videos_video        (title + description + tags)
   - videos_videoframe   (labels, description)
   - videos_videosegment (text)
"""

from django.db import migrations
import pgvector.django


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0016_add_detectedface_composite_indexes'),
    ]

    operations = [

        # ── 1. Enable pgvector extension ─────────────────────────────────────
        migrations.RunSQL(
            sql='CREATE EXTENSION IF NOT EXISTS vector;',
            reverse_sql=migrations.RunSQL.noop,
        ),

        # ── 2a. Allow NULLs before type conversion ────────────────────────────
        migrations.RunSQL(
            sql='ALTER TABLE videos_videoframe ALTER COLUMN clip_embedding DROP NOT NULL;',
            reverse_sql='ALTER TABLE videos_videoframe ALTER COLUMN clip_embedding SET NOT NULL;',
        ),

        # ── 2b. Convert TEXT → vector(512) — empty string becomes NULL ────────
        migrations.RunSQL(
            sql="""
                ALTER TABLE videos_videoframe
                    ALTER COLUMN clip_embedding
                    TYPE vector(512)
                    USING CASE
                        WHEN clip_embedding IS NULL OR trim(clip_embedding) = ''
                        THEN NULL
                        ELSE clip_embedding::vector(512)
                    END;
            """,
            reverse_sql="""
                ALTER TABLE videos_videoframe
                    ALTER COLUMN clip_embedding
                    TYPE text
                    USING CASE
                        WHEN clip_embedding IS NULL THEN ''
                        ELSE clip_embedding::text
                    END;
            """,
        ),

        # ── 2c. Tell Django's migration state about the field change ──────────
        migrations.SeparateDatabaseAndState(
            database_operations=[],   # DDL already done above via RunSQL
            state_operations=[
                migrations.AlterField(
                    model_name='videoframe',
                    name='clip_embedding',
                    field=pgvector.django.VectorField(
                        blank=True,
                        null=True,
                        dimensions=512,
                        help_text='CLIP image embedding (512-dim float vector)',
                    ),
                ),
            ],
        ),

        # ── 3. HNSW index for ANN cosine search ──────────────────────────────
        # m=16, ef_construction=64 are safe defaults; tune after profiling.
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS vf_clip_hnsw
                    ON videos_videoframe
                    USING hnsw (clip_embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64);
            """,
            reverse_sql='DROP INDEX IF EXISTS vf_clip_hnsw;',
        ),

        # ── 4. GIN full-text search indexes ──────────────────────────────────
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS v_title_fts
                    ON videos_video
                    USING GIN (to_tsvector('english',
                        coalesce(title, '') || ' ' ||
                        coalesce(description, '') || ' ' ||
                        coalesce(tags, '')
                    ));

                CREATE INDEX IF NOT EXISTS vf_labels_fts
                    ON videos_videoframe
                    USING GIN (to_tsvector('english', coalesce(labels, '')));

                CREATE INDEX IF NOT EXISTS vf_desc_fts
                    ON videos_videoframe
                    USING GIN (to_tsvector('english', coalesce(description, '')));

                CREATE INDEX IF NOT EXISTS vs_text_fts
                    ON videos_videosegment
                    USING GIN (to_tsvector('english', coalesce(text, '')));
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS v_title_fts;
                DROP INDEX IF EXISTS vf_labels_fts;
                DROP INDEX IF EXISTS vf_desc_fts;
                DROP INDEX IF EXISTS vs_text_fts;
            """,
        ),
    ]
