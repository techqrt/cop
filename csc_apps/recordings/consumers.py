"""WS /recordings/<recording_id>/transcript/ (docs/phase10-live-voice-agent.md
§5, §18). Thin by design: authentication, recording identification, message
validation, connection lifecycle, and error handling live here; the actual
transcript -> eDAR processing is entirely delegated to
csc_apps.recordings.voice_service (the existing eDAR core's adapter) - no
Gemini/extraction business logic in this file.

A sync consumer (`JsonWebsocketConsumer`), not async: every service this
consumer calls (Django's ORM, the existing Sarvam/Gemini providers) is
synchronous, and Channels already runs a sync consumer's event handlers in a
worker thread - reusing that, rather than wrapping every call in
`database_sync_to_async`, is the smallest fit for the existing, entirely
synchronous service layer.
"""

import json
from urllib.parse import parse_qs

from channels.generic.websocket import JsonWebsocketConsumer
from rest_framework.exceptions import AuthenticationFailed

from csc_apps.authentication.authentication import verify_access_token
from csc_apps.common.utils import Utils
from csc_apps.recordings import voice_service

# Custom WebSocket close codes (RFC 6455's private-use range, 4000-4999),
# chosen to mirror the equivalent HTTP status this project already uses for
# the same situation on every other authenticated endpoint (401/403/404) -
# not a new error-code convention.
_CLOSE_UNAUTHENTICATED = 4401
_CLOSE_FORBIDDEN = 4403
_CLOSE_NOT_FOUND = 4404


class TranscriptConsumer(JsonWebsocketConsumer):
    def connect(self):
        recording_id = self.scope['url_route']['kwargs']['recording_id']

        # Browsers cannot set a custom Authorization header on a WebSocket
        # handshake (a real, well-known limitation of the browser WebSocket
        # API, which Flutter Web inherits) - the same JWT POST /auth/login/
        # issues travels as a query parameter instead: `?token=<jwt>`.
        token = _token_from_query_string(self.scope.get('query_string', b''))
        if not token:
            self.close(code=_CLOSE_UNAUTHENTICATED)
            return

        try:
            # Reuses the exact JWT verification csc_apps.authentication.
            # authentication.JWTAuthentication uses for every HTTP endpoint -
            # not a second, WebSocket-only authentication implementation
            # (task §15).
            user, _payload = verify_access_token(token)
        except AuthenticationFailed:
            self.close(code=_CLOSE_UNAUTHENTICATED)
            return

        try:
            # Reuses the exact owner-or-REVIEWER/ADMIN rule every other
            # recording endpoint uses (task §27) - never a different
            # permission model for the WebSocket.
            recording = voice_service.authorize_recording_for_voice(user, recording_id)
        except voice_service.RecordingNotFoundError:
            self.close(code=_CLOSE_NOT_FOUND)
            return
        except voice_service.NotAuthorizedError:
            self.close(code=_CLOSE_FORBIDDEN)
            return

        self.voice_session = voice_service.start_voice_session(recording, user)
        self.accept()

    def disconnect(self, close_code):
        voice_session = getattr(self, 'voice_session', None)
        if voice_session is not None:
            voice_service.end_voice_session(voice_session)

    def receive(self, text_data=None, bytes_data=None, **kwargs):
        # JsonWebsocketConsumer.receive's own implementation calls
        # self.decode_json() (a bare json.loads) before receive_json() ever
        # runs, so a malformed (non-JSON) frame previously raised
        # JSONDecodeError here, uncaught - Channels' default handling for an
        # unhandled consumer exception is to close the socket (code 1011),
        # ending the officer's whole conversation over one bad frame (real,
        # live-verified defect, 2026-09: an E2E test sending a plain string
        # instead of JSON killed the connection outright). Same principle
        # receive_json's own try/except already applies to processing
        # failures - "one bad message must not tear down the connection" -
        # just extended to cover decoding itself, not only downstream
        # processing.
        if not text_data:
            self._send_error('Binary WebSocket frames are not supported')
            return
        try:
            content = self.decode_json(text_data)
        except json.JSONDecodeError:
            self._send_error('Message must be valid JSON')
            return
        self.receive_json(content, **kwargs)

    def receive_json(self, content, **kwargs):
        # Valid JSON but the wrong shape (e.g. a JSON array or a bare string)
        # would otherwise crash `.get()` below the same way malformed JSON
        # crashed `decode_json` above - same fix, same reasoning.
        if not isinstance(content, dict):
            self._send_error('Message must be a JSON object')
            return

        interaction_id = content.get('interactionId')
        transcript = content.get('transcript')
        language_code = content.get('languageCode')

        # Only fields the existing backend actually requires are validated
        # (task §5) - no invented payload requirements.
        if not isinstance(interaction_id, str) or not interaction_id:
            self._send_error('interactionId is required')
            return
        if not isinstance(transcript, str):
            self._send_error('transcript is required')
            return
        if language_code is not None and not isinstance(language_code, str):
            self._send_error('languageCode must be a string')
            return

        try:
            result = voice_service.process_transcript(
                self.voice_session, interaction_id=interaction_id, transcript=transcript,
                language_code=language_code,
            )
        except Exception as e:  # noqa: BLE001 - one malformed/unexpected transcript
            # must not tear down the whole connection; report it and keep
            # listening for the next one. Same debug-gated message exposure
            # as Common.exception_handler (docs/phase9-security-audit-
            # observability.md) - never a raw stack trace, never a secret.
            self._send_error('Unable to process transcript', detail=Utils.env_exception_handler(message=str(e)))
            return

        # No eDAR payload and no "next question" here (task §12) - only an
        # acknowledgement. Flutter/Sarvam fetch current state and
        # missingFields from the existing GET /recordings/<id>/ endpoint.
        self.send_json({
            'status': True,
            'message': 'Transcript processed',
            'data': {
                'interactionId': interaction_id,
                'resolvedFieldCount': result['resolvedFieldCount'],
                'resolvedFieldKeys': result['resolvedFieldKeys'],
            },
        })

    def _send_error(self, message: str, detail: str | None = None) -> None:
        self.send_json({'status': False, 'message': message, 'error': [detail] if detail else []})


def _token_from_query_string(raw_query_string: bytes) -> str | None:
    params = parse_qs(raw_query_string.decode('utf-8'))
    values = params.get('token')
    return values[0] if values else None
