from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(), name='swagger-ui'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(), name='redoc'),

    # ----------------------------
    # Auth
    # ----------------------------
    path('auth/', include('csc_apps.authentication.urls')),

    # ----------------------------
    # Recordings - audio ingestion only (Phase 1, docs/phase1-audio-ingestion.md).
    # Review/eDAR/export endpoints remain undocumented API surface, see
    # docs/api-architecture.md.
    # ----------------------------
    path('recordings/', include('csc_apps.recordings.urls')),
    # path('activity/', include('csc_apps.activity_log.urls')),
]

if settings.DEBUG:
    # Local-dev convenience only. No endpoint relies on this public static() mount -
    # audio is stored under PRIVATE_STORAGE_ROOT, never MEDIA_ROOT (docs/security-
    # baseline.md §3, csc_apps.recordings.storage.LocalPrivateAudioStorage).
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
