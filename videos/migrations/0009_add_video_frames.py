from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0008_add_video_segments'),
    ]

    operations = [
        migrations.CreateModel(
            name='VideoFrame',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp',   models.FloatField(help_text='Seconds into the video')),
                ('labels',      models.TextField(blank=True, help_text='Comma-separated YOLO class labels')),
                ('face_count',  models.IntegerField(default=0, help_text='Number of person detections')),
                ('face_names',  models.TextField(blank=True, help_text='Identified face names (Phase B)')),
                ('description', models.TextField(blank=True, help_text='Scene description (Phase C)')),
                ('video', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='frames',
                    to='videos.video',
                )),
            ],
            options={
                'ordering': ['timestamp'],
                'indexes': [
                    models.Index(fields=['video', 'timestamp'], name='videos_vide_video_i_frames_idx'),
                ],
            },
        ),
    ]
