from django.urls import path

from csc_apps.recordings.controller import RecordingViewController

urlpatterns = [
    path('', RecordingViewController.recordings_root, name='recordings_root'),
    # Registered before the <int:recording_id>/ pattern below on principle (a
    # literal segment ahead of a parameterized one) - the int path converter
    # cannot match "get_all" anyway, so this ordering isn't load-bearing for
    # correctness, only clarity.
    path('get_all/', RecordingViewController.get_all, name='recording_get_all'),
    path('<int:recording_id>/', RecordingViewController.recording_detail_root, name='recording_detail'),
    path('<int:recording_id>/edar/approve/', RecordingViewController.approve_edar, name='recording_edar_approve'),
    path('<int:recording_id>/export/', RecordingViewController.export_edar, name='recording_export'),
]
