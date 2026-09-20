import jwt
from rest_framework import status
from rest_framework.response import Response

from csc.constants import Constants
from csc_apps.common.exceptions.token_errors import TokenErrors
from csc_apps.common.exceptions.validation_errors import ValidationErrors
from csc_apps.common.utils import Utils


class Common:
    """Central exception-handling decorator. Ported from pms_apps/common/common.py
    (docs/pms-reference-analysis.md §6, §8) - every view method funnels its errors
    through this one place rather than scattering try/except across the codebase."""

    def __init__(self, response_handler=None):
        self.db_error = 'Database Error'
        self.error = 'Something went wrong'
        self.token_error = 'Unauthorized'
        self.foreign_key_error = 'This action is blocked because the record is referenced by other records'
        self.response_handler = response_handler

    def exception_handler(self, func):
        def exceptions(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if self.response_handler is not None:
                    serializer = self.response_handler(data=result.data)
                    serializer.is_valid(raise_exception=True)
                return result
            except ValueError as e:
                return Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data=Utils.error_response_data(message='Value Error ' + str(e), error=[str(e)]),
                )
            except ValidationErrors as e:
                return Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data=Utils.error_response_data(message=Constants.validation_error, error=e.errors),
                )
            except TokenErrors as e:
                return Response(
                    status=status.HTTP_401_UNAUTHORIZED,
                    data=Utils.error_response_data(message=self.token_error, error=e.errors),
                )
            except jwt.exceptions.InvalidSignatureError:
                return Response(
                    status=status.HTTP_401_UNAUTHORIZED,
                    data=Utils.error_response_data(
                        message=self.token_error,
                        error=['Signature verification failed', 'Logged in on another device'],
                    ),
                )
            except Exception as e:
                if 'foreign key constraint' in str(e):
                    return Response(
                        status=status.HTTP_400_BAD_REQUEST,
                        data=Utils.error_response_data(
                            error=[self.foreign_key_error],
                            message='Foreign Key Error ' + Utils.env_exception_handler(message=str(e)),
                        ),
                    )
                return Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data=Utils.error_response_data(
                        error=[self.db_error],
                        message='Exception Error ' + Utils.env_exception_handler(message=str(e)),
                    ),
                )

        return exceptions
