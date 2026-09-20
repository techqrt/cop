import uuid
from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from csc.config import Configurations
from csc.constants import Constants
from csc_apps.authentication.dataclasses.request.login import LoginRequest
from csc_apps.authentication.models import User
from csc_apps.authentication.serializers.response.login import LoginResponseSerializer
from csc_apps.common.common import Common
from csc_apps.common.utils import Utils


class AuthView:
    def __init__(self):
        self.data_login = 'Authentication successful'
        self.invalid_credentials = Constants.invalid_credentials

    def _issue_token(self, user: User) -> str:
        payload = {
            'user_id': user.user_id,
            'role': user.role,
            'exp': datetime.now(timezone.utc) + timedelta(days=Configurations.access_token_lifetime_days),
            'iat': datetime.now(timezone.utc),
            # Two logins within the same second would otherwise produce a
            # byte-identical token (exp/iat only have second precision), which would
            # defeat single-active-session invalidation (docs/security-baseline.md §1)
            # since the "old" and "new" token would be the same string.
            'jti': uuid.uuid4().hex,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')

    @Common(response_handler=LoginResponseSerializer).exception_handler
    def login_extract(self, params: LoginRequest) -> Response:
        with transaction.atomic():
            user = User.objects.filter(email=params.email, is_active=True).first()
            if user is None or not user.check_password(params.password):
                raise ValueError(self.invalid_credentials)

            token = self._issue_token(user)
            # Overwriting access_token here is what invalidates every token issued
            # before this login (docs/security-baseline.md §1).
            User.objects.filter(user_id=user.user_id).update(access_token=token)

        return Response(
            status=status.HTTP_200_OK,
            data=Utils.success_response_data(
                message=self.data_login,
                data={
                    'token': token,
                    'user': {
                        'userId': user.user_id,
                        'email': user.email,
                        'name': user.name,
                        'role': user.role,
                    },
                },
            ),
        )
