from django.db import migrations, models


class Migration(migrations.Migration):
    # Required for CREATE INDEX CONCURRENTLY (cannot run inside a transaction)
    atomic = False

    dependencies = [
        ('videos', '0020_albums_photo_updates'),
    ]

    operations = [
        migrations.AddField(
            model_name='photo',
            name='ocr_text',
            field=models.TextField(blank=True, help_text='Text detected in image via OCR'),
        ),
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS photo_ocr_text_gin
                ON videos_photo
                USING gin(to_tsvector('english', ocr_text));
            """,
            reverse_sql="DROP INDEX IF EXISTS photo_ocr_text_gin;",
        ),
    ]
