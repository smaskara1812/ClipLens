"""
Migration 0018 — pg_trgm extension + trigram indexes for fuzzy search.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0017_pgvector_and_fts'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE EXTENSION IF NOT EXISTS pg_trgm;',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS v_title_trgm
                    ON videos_video USING GIN (title gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS v_description_trgm
                    ON videos_video USING GIN (description gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS v_tags_trgm
                    ON videos_video USING GIN (tags gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS vs_text_trgm
                    ON videos_videosegment USING GIN (text gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS vf_labels_trgm
                    ON videos_videoframe USING GIN (labels gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS vf_description_trgm
                    ON videos_videoframe USING GIN (description gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS ch_name_trgm
                    ON videos_channel USING GIN (name gin_trgm_ops);
                CREATE INDEX IF NOT EXISTS pl_title_trgm
                    ON videos_playlist USING GIN (title gin_trgm_ops);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS v_title_trgm;
                DROP INDEX IF EXISTS v_description_trgm;
                DROP INDEX IF EXISTS v_tags_trgm;
                DROP INDEX IF EXISTS vs_text_trgm;
                DROP INDEX IF EXISTS vf_labels_trgm;
                DROP INDEX IF EXISTS vf_description_trgm;
                DROP INDEX IF EXISTS ch_name_trgm;
                DROP INDEX IF EXISTS pl_title_trgm;
            """,
        ),
    ]
