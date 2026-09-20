from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from csc_apps.common.serializer_validations import SerializerValidations
from csc_apps.authentication.serializers.request.login import LoginRequestSerializer
from csc_apps.authentication.serializers.response.login import LoginResponseSerializer
from csc_apps.authentication.views import AuthView


class AuthViewController:

    @extend_schema(
        description='Log in with email and password',
        request=LoginRequestSerializer,
        responses=LoginResponseSerializer,
    )
    @api_view(['POST'])
    @SerializerValidations(serializer=LoginRequestSerializer, require_auth=False).validate
    def login(request: Request) -> Response:
        return AuthView().login_extract(params=request.params)
