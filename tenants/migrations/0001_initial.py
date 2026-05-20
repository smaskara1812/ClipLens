from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Plan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=80, unique=True)),
                ('storage_limit_gb', models.PositiveIntegerField(default=100)),
                ('ai_minutes_limit', models.PositiveIntegerField(default=300)),
                ('max_users', models.PositiveIntegerField(default=3)),
                ('max_videos', models.PositiveIntegerField(default=0, help_text='0 = unlimited')),
            ],
            options={'app_label': 'tenants'},
        ),
        migrations.CreateModel(
            name='Tenant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=60, unique=True)),
                ('name', models.CharField(max_length=200)),
                ('db_name', models.CharField(max_length=100, unique=True)),
                ('media_folder', models.CharField(max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('admin_email', models.EmailField(blank=True)),
                ('plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='tenants',
                    to='tenants.plan',
                )),
            ],
            options={'app_label': 'tenants'},
        ),
        migrations.CreateModel(
            name='UsageEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(
                    choices=[
                        ('video_processing', 'Video Processing'),
                        ('photo_processing', 'Photo Processing'),
                        ('translation', 'Translation'),
                        ('storage_delta', 'Storage Delta'),
                    ],
                    max_length=30,
                )),
                ('value', models.FloatField(default=0)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('task_id', models.CharField(blank=True, max_length=100)),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='usage_events',
                    to='tenants.tenant',
                )),
            ],
            options={'app_label': 'tenants'},
        ),
        migrations.AddIndex(
            model_name='usageevent',
            index=models.Index(
                fields=['tenant', 'event_type', 'timestamp'],
                name='tenants_usa_tenant__idx',
            ),
        ),
    ]
