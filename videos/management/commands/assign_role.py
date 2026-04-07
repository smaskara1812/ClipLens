"""
Management command: assign_role

Assigns a ClipStream role (viewer / editor / superadmin) to one or more users.

Usage examples
--------------
  # Make a single user an editor
  python manage.py assign_role --username soham --role editor

  # Make multiple users editors
  python manage.py assign_role --username soham --username priya --role editor

  # Promote a user to superadmin (also sets is_staff + is_superuser so they
  # can access the Django admin portal)
  python manage.py assign_role --username soham --role superadmin

  # Demote back to viewer
  python manage.py assign_role --username soham --role viewer

  # List every user and their current role
  python manage.py assign_role --list
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from videos.models import UserProfile


ROLES = [UserProfile.ROLE_VIEWER, UserProfile.ROLE_EDITOR, UserProfile.ROLE_SUPERADMIN]


class Command(BaseCommand):
    help = 'Assign or view roles for ClipStream users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username', dest='usernames', action='append', metavar='USERNAME',
            help='Username(s) to update. Can be specified multiple times.',
        )
        parser.add_argument(
            '--role', choices=ROLES,
            help=f'Role to assign: {" | ".join(ROLES)}',
        )
        parser.add_argument(
            '--list', action='store_true',
            help='Print all users and their current roles, then exit.',
        )

    def handle(self, *args, **options):
        if options['list']:
            self._list_roles()
            return

        usernames = options.get('usernames') or []
        role = options.get('role')

        if not usernames:
            raise CommandError('Provide at least one --username.')
        if not role:
            raise CommandError('Provide a --role (viewer / editor / superadmin).')

        for username in usernames:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'  ✗ User "{username}" not found.'))
                continue

            profile, created = UserProfile.objects.get_or_create(
                user=user, defaults={'role': UserProfile.ROLE_VIEWER}
            )
            old_role = profile.role
            profile.role = role
            profile.save(update_fields=['role'])

            # Superadmin → also grant Django admin access
            if role == UserProfile.ROLE_SUPERADMIN:
                user.is_staff = True
                user.is_superuser = True
                user.save(update_fields=['is_staff', 'is_superuser'])
                self.stdout.write(self.style.WARNING(
                    f'  ⚠ {username}: is_staff + is_superuser set to True (Django admin access granted).'
                ))
            # If demoting from superadmin, optionally strip Django admin access
            elif old_role == UserProfile.ROLE_SUPERADMIN and role != UserProfile.ROLE_SUPERADMIN:
                user.is_staff = False
                user.is_superuser = False
                user.save(update_fields=['is_staff', 'is_superuser'])
                self.stdout.write(self.style.WARNING(
                    f'  ⚠ {username}: Django admin access (is_staff, is_superuser) removed.'
                ))

            self.stdout.write(self.style.SUCCESS(
                f'  ✓ {username}: {old_role} → {role}'
            ))

    def _list_roles(self):
        self.stdout.write(self.style.MIGRATE_HEADING('\n  Username                  Role        Django Admin'))
        self.stdout.write('  ' + '─' * 56)
        for user in User.objects.prefetch_related('profile').order_by('username'):
            try:
                role = user.profile.role
            except Exception:
                role = '(no profile)'
            admin_flag = '✓' if user.is_superuser else ''
            self.stdout.write(f'  {user.username:<26}{role:<12}{admin_flag}')
        self.stdout.write('')
