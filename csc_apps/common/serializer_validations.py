from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from csc.constants import Constants
from csc_apps.common.utils import Utils


class SerializerValidations:
    """Ported from pms_apps/common/serializer_validations.py
    (docs/pms-reference-analysis.md §3, §5). Validates the incoming request body+query
    against `serializer`, then attaches the resulting typed dataclass to request.params.

    `require_auth=False` is used only for endpoints that authenticate the caller
    themselves (e.g. login) - every other endpoint requires an already-authenticated
    request.user, same as PMS.
    """

    def __init__(self, serializer, require_auth: bool = True):
        self.validation_error = Constants.validation_error
        self.serializer = serializer
        self.require_auth = require_auth

    def validate(self, func):
        def validator(*args, **kwargs):
            request: Request = args[0]

            if self.require_auth and not (request.user and request.user.is_authenticated):
                return Response(
                    status=status.HTTP_401_UNAUTHORIZED,
                    data=Utils.error_response_data(
                        message='Unauthorized',
                        error=['Authentication credentials were not provided or are invalid'],
                    ),
                )

            data = request.data
            if hasattr(data, '_mutable'):
                data = data.copy()
            data.update(Utils.get_query_params(request=request))
            serializer = self.serializer(data=data)
            validated = Utils().validator(serializer=serializer)
            if isinstance(validated, bool):
                params = serializer.create(serializer.validated_data)
                request.params = params
                if request.method == 'GET':
                    request.params.present_url = request.build_absolute_uri()
                if self.require_auth:
                    request.params.user_id = request.user.user_id
                return func(*args, **kwargs)
            return validated

        return validator
