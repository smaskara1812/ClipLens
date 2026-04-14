from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0032_videomoment_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='seek_sprite',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
