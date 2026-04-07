"""
Management command: rename_auto_identities
==========================================
Renames all auto-named FaceIdentity rows that still use the old per-video
"Person N" scheme to the globally unique "Person #<pk>" scheme.

Only rows where is_auto_named=True and name matches "Person <digits>" are
touched. Named identities (is_auto_named=False) are never changed.

Usage:
    python manage.py rename_auto_identities
    python manage.py rename_auto_identities --dry-run
"""

import re
from django.core.management.base import BaseCommand
from videos.models import FaceIdentity

OLD_PATTERN = re.compile(r'^Person \d+$')


class Command(BaseCommand):
    help = 'Rename auto-named "Person N" identities to globally unique "Person #<pk>"'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Preview changes without writing to the database.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        qs = FaceIdentity.objects.filter(is_auto_named=True)
        to_rename = [fi for fi in qs if OLD_PATTERN.match(fi.name)]

        self.stdout.write(f'\n{"[DRY RUN] " if dry_run else ""}Found {len(to_rename)} identities to rename.\n')

        updated = 0
        for fi in to_rename:
            new_name = f'Person #{fi.pk}'
            self.stdout.write(f'  {fi.pk:>6}  "{fi.name}" → "{new_name}"')
            if not dry_run:
                fi.name = new_name
                fi.save(update_fields=['name'])
            updated += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run — no changes made.\n'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {updated} identities renamed.\n'))
