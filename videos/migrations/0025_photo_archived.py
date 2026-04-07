from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0024_rename_videos_detec_photo_i_75b1c9_idx_videos_dete_photo_i_0fcd4a_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='photo',
            name='is_archived',
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text='Archived photos are hidden from the main library but not deleted',
            ),
        ),
    ]
