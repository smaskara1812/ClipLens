"""Signal handlers for authentication auditing."""
from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver

from .activity import log_activity


@receiver(user_logged_in)
def _audit_login(sender, request, user, **kwargs):
    log_activity(request, 'login', target=user, verb='logged in', actor=user)


@receiver(user_logged_out)
def _audit_logout(sender, request, user, **kwargs):
    # user can be None if the session was already invalid
    if user is None:
        return
    log_activity(request, 'logout', target=user, verb='logged out', actor=user)


@receiver(user_login_failed)
def _audit_login_failed(sender, credentials, request, **kwargs):
    username = ''
    if credentials:
        username = credentials.get('username') or credentials.get('email') or ''
    log_activity(
        request, 'login_failed',
        verb='failed login',
        target_type='User',
        target_label=username[:500],
        metadata={'attempted_username': username},
    )
