from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0022_alter_album_id_alter_album_share_token_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='detectedface',
            name='video',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='detected_faces',
                to='videos.video',
            ),
        ),
        migrations.AddField(
            model_name='detectedface',
            name='photo',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='detected_faces',
                to='videos.photo',
            ),
        ),
        migrations.AddIndex(
            model_name='detectedface',
            index=models.Index(fields=['photo', 'timestamp'], name='videos_detec_photo_i_75b1c9_idx'),
        ),
        migrations.AddIndex(
            model_name='detectedface',
            index=models.Index(fields=['identity', 'photo'], name='detface_identity_photo'),
        ),
    ]
