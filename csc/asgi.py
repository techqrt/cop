import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'csc.settings')

# django.setup() (implicitly run by get_asgi_application(), below) must
# complete before anything imports Django models - so the Channels-specific
# imports and app-provided routing are deferred until after that call, same
# ordering Channels' own documentation prescribes.
from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

import csc_apps.recordings.routing  # noqa: E402

# Phase 10 - Live Voice Agent (docs/phase10-live-voice-agent.md §WebSocket
# design). Every existing HTTP endpoint is untouched - `http` still routes to
# the exact same Django application WSGI serves (csc.wsgi.application);
# `websocket` is the one new protocol, routed to
# csc_apps.recordings.routing.websocket_urlpatterns. No AuthMiddlewareStack -
# that's Channels' session/cookie-based auth, which doesn't fit this
# project's JWT scheme; the transcript consumer authenticates itself
# (csc_apps.recordings.consumers.TranscriptConsumer.connect), the same
# "authenticates the caller itself" pattern already used elsewhere.
application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': URLRouter(csc_apps.recordings.routing.websocket_urlpatterns),
})
