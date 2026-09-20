from django.urls import path

from csc_apps.authentication.controller import AuthViewController

urlpatterns = [
    path('login/', AuthViewController.login, name='auth_login'),
]
