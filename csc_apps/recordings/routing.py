from django.urls import path

from csc_apps.recordings.consumers import TranscriptConsumer

# Phase 10 - Live Voice Agent (docs/phase10-live-voice-agent.md §5). This
# project had no established WebSocket routing convention before this phase
# (verified: no channels/ASGI routing existed at all) - the path below
# follows the task's own given conceptual route,
# `WS /recordings/<recording_id>/transcript/`, unchanged, rather than
# inventing a different convention (e.g. a `/ws/` prefix) nothing in the
# repository or the task asked for. This is a separate router namespace from
# csc_apps.recordings.urls (HTTP) - the string overlap with
# `recordings/<id>/...` is not a literal collision, Channels' URLRouter and
# Django's URLconf are wired to different protocols entirely (csc/asgi.py).
websocket_urlpatterns = [
    path('recordings/<int:recording_id>/transcript/', TranscriptConsumer.as_asgi()),
]
