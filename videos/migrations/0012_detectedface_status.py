from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0011_add_clip_embedding'),
    ]

    operations = [
        migrations.AddField(
            model_name='detectedface',
            name='status',
            field=models.CharField(
                choices=[
                    ('unreviewed', 'Unreviewed'),
                    ('confirmed',  'Confirmed'),
                    ('rejected',   'Rejected'),
                ],
                default='unreviewed',
                help_text='Manual review status: unreviewed/confirmed/rejected',
                max_length=20,
            ),
        ),
    ]
