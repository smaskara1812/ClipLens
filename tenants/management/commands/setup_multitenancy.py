"""
Management command: setup_multitenancy
──────────────────────────────────────
One-time setup for multi-tenant mode.

Usage:
    python manage.py setup_multitenancy

What it does:
    1. Creates the freestream_control PostgreSQL database
    2. Enables pgvector, pg_trgm, unaccent extensions
    3. Registers the 'control' alias in settings.DATABASES
    4. Runs Django migrations for the tenants app against the control DB
    5. Optionally seeds starter plans

Run this ONCE after setting MULTI_TENANT=true in your .env file.
"""

import psycopg2
from psycopg2 import sql as pg_sql

from django.core.management.base import BaseCommand
from django.conf import settings
from django.core import management


class Command(BaseCommand):
    help = 'One-time setup: create control DB and migrate tenants app into it.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--seed-plans',
            action='store_true',
            default=True,
            help='Seed default Starter/Pro/Enterprise plans (default: True)',
        )
        parser.add_argument(
            '--no-seed-plans',
            dest='seed_plans',
            action='store_false',
        )
        parser.add_argument(
            '--platform-owner-username',
            default='',
            help='Username of the platform owner to grant is_platform_owner=True',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'MULTI_TENANT', False):
            self.stderr.write(self.style.ERROR(
                "MULTI_TENANT is not set to true in your .env file.\n"
                "Add MULTI_TENANT=true and restart, then re-run this command."
            ))
            return

        ctrl_cfg = settings.DATABASES.get('control')
        if not ctrl_cfg:
            self.stderr.write(self.style.ERROR(
                "No 'control' database found in settings.DATABASES.\n"
                "Make sure MULTI_TENANT=true and the server has been restarted."
            ))
            return

        db_name = ctrl_cfg['NAME']

        # ── Step 1: Create the control DB ──────────────────────────────────────
        self.stdout.write(f"Creating database '{db_name}'...")
        conn = psycopg2.connect(
            dbname='postgres',
            user=ctrl_cfg['USER'],
            password=ctrl_cfg['PASSWORD'],
            host=ctrl_cfg['HOST'],
            port=ctrl_cfg['PORT'],
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone():
                self.stdout.write(self.style.WARNING(f"  Database '{db_name}' already exists — skipping CREATE."))
            else:
                cur.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(db_name)))
                self.stdout.write(self.style.SUCCESS(f"  Created '{db_name}'."))
        conn.close()

        # ── Step 2: Enable extensions ──────────────────────────────────────────
        self.stdout.write("Enabling PostgreSQL extensions...")
        conn2 = psycopg2.connect(
            dbname=db_name,
            user=ctrl_cfg['USER'],
            password=ctrl_cfg['PASSWORD'],
            host=ctrl_cfg['HOST'],
            port=ctrl_cfg['PORT'],
        )
        conn2.autocommit = True
        with conn2.cursor() as cur:
            for ext in ['vector', 'pg_trgm', 'unaccent']:
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext};")
        conn2.close()
        self.stdout.write(self.style.SUCCESS("  Extensions enabled."))

        # ── Step 3: Run migrations for tenants app on control DB ───────────────
        self.stdout.write("Running migrations on control DB...")
        management.call_command('migrate', '--database=control', verbosity=1)
        self.stdout.write(self.style.SUCCESS("  Migrations complete."))

        # ── Step 4: Seed default plans ─────────────────────────────────────────
        if options['seed_plans']:
            self.stdout.write("Seeding default plans...")
            from tenants.models import Plan
            defaults = [
                {'name': 'Starter',    'storage_limit_gb': 50,   'ai_minutes_limit': 100, 'max_users': 2},
                {'name': 'Pro',        'storage_limit_gb': 200,  'ai_minutes_limit': 500, 'max_users': 10},
                {'name': 'Enterprise', 'storage_limit_gb': 1000, 'ai_minutes_limit': 0,   'max_users': 0},
            ]
            for plan_data in defaults:
                _, created = Plan.objects.using('control').get_or_create(
                    name=plan_data['name'], defaults=plan_data
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f"  Created plan: {plan_data['name']}"))
                else:
                    self.stdout.write(f"  Plan '{plan_data['name']}' already exists — skipping.")

        # ── Step 5: Grant platform owner ───────────────────────────────────────
        username = options.get('platform_owner_username', '').strip()
        if username:
            from django.contrib.auth.models import User
            try:
                user = User.objects.get(username=username)
                profile = user.userprofile
                profile.is_platform_owner = True
                profile.save()
                self.stdout.write(self.style.SUCCESS(
                    f"  Granted is_platform_owner to '{username}'."
                ))
            except User.DoesNotExist:
                self.stderr.write(self.style.WARNING(
                    f"  User '{username}' not found in default DB — skipping."
                ))

        self.stdout.write(self.style.SUCCESS("\n✓ Multi-tenancy setup complete!"))
        self.stdout.write("Next steps:")
        self.stdout.write("  1. Add *.cliplens.local to /etc/hosts (see docs/multitenancy.md)")
        self.stdout.write("  2. Configure local nginx to route subdomains → :8000")
        self.stdout.write("  3. Visit http://localhost:8000/platform/ to create your first org")
