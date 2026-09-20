import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from csc.constants import Constants
from csc_apps.authentication.models import User


class JWTAuthentication(BaseAuthentication):
    """Ported from pms_apps/authentication/authentication.py
    (docs/pms-reference-analysis.md §7, docs/security-baseline.md §1).

    Verifies the Bearer token's signature *and* that it matches the token currently
    stored on the User row - a second login overwrites User.access_token, which
    immediately invalidates every token issued before it (single active session,
    with no separate blacklist to maintain).

    PMS's department-based `validate_permissions` path-matching is not ported: CSC's
    role model is flat (docs/security-baseline.md §2) and no endpoint beyond login
    exists yet to gate (docs/api-architecture.md §3) - per-endpoint role checks are
    added alongside the endpoints that need them in Phase 1.
    """

    def authenticate(self, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None

        token = auth_header.split(' ', 1)[1]
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed(Constants.access_token_expired)
        except jwt.InvalidTokenError:
            raise AuthenticationFailed(Constants.invalid_header)

        user_row = User.get(user_id=payload.get('user_id'))
        if user_row is None:
            raise AuthenticationFailed('User not found')
        if not user_row['is_active']:
            raise AuthenticationFailed(Constants.forbidden_access)
        if token != user_row['access_token']:
            raise AuthenticationFailed(Constants.invalid_access_token)

        try:
            user = User.objects.get(user_id=payload['user_id'])
        except User.DoesNotExist:
            raise AuthenticationFailed('User not found')

        if payload.get('role') and payload['role'] != user.role:
            raise AuthenticationFailed('Token role mismatch')

        request.payload = payload
        return user, payload
