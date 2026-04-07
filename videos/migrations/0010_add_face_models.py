from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0009_add_video_frames'),
    ]

    operations = [
        migrations.CreateModel(
            name='FaceIdentity',
            fields=[
                ('id',           models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name',         models.CharField(max_length=100)),
                ('is_auto_named',models.BooleanField(default=True, help_text='True until owner gives this identity a real name')),
                ('ref_embedding',models.TextField(blank=True, help_text='JSON float array — running average of all face embeddings')),
                ('thumbnail',    models.CharField(blank=True, max_length=500, help_text='Relative path to a representative face-crop JPEG')),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['name']},
        ),
        migrations.CreateModel(
            name='DetectedFace',
            fields=[
                ('id',         models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp',  models.FloatField(help_text='Seconds into the video')),
                ('bbox',       models.TextField(help_text='JSON [x1,y1,x2,y2] pixel coords')),
                ('embedding',  models.TextField(blank=True, help_text='JSON float[512] — InsightFace ArcFace embedding')),
                ('confidence', models.FloatField(default=0.0, help_text='InsightFace detection score (0–1)')),
                ('crop_path',  models.CharField(blank=True, max_length=500, help_text='Relative path to cropped face JPEG in media/')),
                ('video',      models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='detected_faces', to='videos.video')),
                ('frame',      models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='faces', to='videos.videoframe')),
                ('identity',   models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='faces', to='videos.faceidentity')),
            ],
            options={'ordering': ['timestamp']},
        ),
        migrations.AddIndex(
            model_name='detectedface',
            index=models.Index(fields=['video', 'timestamp'], name='videos_dete_video_i_f9b3c2_idx'),
        ),
        migrations.AddIndex(
            model_name='detectedface',
            index=models.Index(fields=['identity'], name='videos_dete_identit_b7a1c3_idx'),
        ),
    ]
