from urllib.parse import unquote_plus

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from csc.config import Configurations
from csc.constants import Constants


class Utils:
    """Ported from pms_apps/common/utils.py (docs/pms-reference-analysis.md §6)."""

    def __init__(self):
        self.validation_error = Constants.validation_error

    @staticmethod
    def success_response_data(message, data: list | dict = None):
        if data is None and message is None:
            return {'status': True}
        if message is None:
            return {'status': True, 'data': data}
        if data is None:
            return {'status': True, 'message': message}
        return {'status': True, 'message': message, 'data': data}

    @staticmethod
    def error_response_data(message: str, error: list[str]):
        return {'status': False, 'message': message, 'error': error}

    @staticmethod
    def env_exception_handler(message: str):
        if Configurations.debug:
            return message
        return Constants.server_error

    @staticmethod
    def add_page_parameter(
        final_data: list,
        page_num: int,
        total_page: int,
        present_url: str,
        next_page_required: bool = False,
    ):
        to_return = {
            'data': final_data,
            'presentPage': page_num,
            'totalPage': total_page,
        }

        if total_page > 1:
            if 'page_num' in present_url:
                base_next_url = present_url
            elif '?' in present_url:
                base_next_url = present_url + '&page_num=' + str(page_num)
            else:
                base_next_url = present_url + '?page_num=' + str(page_num)

            if next_page_required and page_num < total_page:
                to_return['nextPageUrl'] = base_next_url.replace(
                    f'page_num={page_num}', f'page_num={page_num + 1}'
                )

            if page_num > 1:
                to_return['previousPageUrl'] = base_next_url.replace(
                    f'page_num={page_num}', f'page_num={page_num - 1}'
                )

        return to_return

    @staticmethod
    def extract_params(url: str):
        query = url.split('?')
        info = query[1] if len(query) > 1 else 'page_num=1'
        return info.split('&'), query[0]

    @staticmethod
    def get_query_params(request: Request):
        query_params = {}
        try:
            url = request.get_full_path()
        except Exception:
            url = request.path
        query, _base_url = Utils.extract_params(url=url)
        for pair in query:
            if '=' in pair:
                key, value = pair.split('=', 1)
            else:
                key, value = pair, ''
            # `request.get_full_path()` returns the raw, still percent-/plus-encoded
            # query string (Django never decodes it for us here) - a filter value
            # containing a space or other reserved character (e.g. Phase 7's
            # `road_name` free-text search, docs/phase7-history-search.md) would
            # otherwise reach the serializer still encoded (`"NH+48"`/`"NH%2048"`
            # instead of `"NH 48"`). No prior query param in this codebase
            # contained a space, so this never surfaced before.
            query_params[unquote_plus(key)] = unquote_plus(value)
        return query_params

    def validator(self, serializer):
        if serializer.is_valid() is False:
            response_data = Utils.error_response_data(
                message=self.validation_error, error=[serializer.errors]
            )
            return Response(response_data, status.HTTP_400_BAD_REQUEST)
        return True
