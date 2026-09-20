from django.urls import path

from csc_apps.recordings.controller import RecordingViewController

urlpatterns = [
    path('', RecordingViewController.upload, name='recording_upload'),
    path('<int:recording_id>/', RecordingViewController.get, name='recording_detail'),
]
