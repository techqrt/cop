from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from csc_apps.authentication.managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    """The single CSC user model (docs/security-baseline.md §2). Flat role field,
    replacing PMS's department-string permission matrix
    (docs/pms-reference-analysis.md §7) - CSC has one domain, not many departments.
    """

    ROLE_CHOICES = [
        ('OFFICER', 'Officer'),
        ('REVIEWER', 'Reviewer'),
        ('ADMIN', 'Admin'),
    ]

    user_id = models.AutoField(primary_key=True)
    email = models.EmailField(verbose_name='Email', unique=True)
    name = models.CharField(verbose_name='Name', max_length=150)
    role = models.CharField(verbose_name='Role', choices=ROLE_CHOICES, max_length=10)

    # Single-active-session token, compared against the presented Bearer token on
    # every request (docs/pms-reference-analysis.md §7, docs/security-baseline.md §1).
    access_token = models.TextField(verbose_name='Access Token', default='', blank=True)
    refresh_token = models.TextField(verbose_name='Refresh Token', default='', blank=True)

    is_active = models.BooleanField(verbose_name='Is Active', default=True)
    is_staff = models.BooleanField(verbose_name='Is Staff', default=False)

    created_at = models.DateTimeField(verbose_name='Created At', auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name='Updated At', auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['name', 'role']

    class Meta:
        db_table = 'users'

    def __str__(self) -> str:
        return f'{self.name} <{self.email}> ({self.role})'

    @staticmethod
    def get(user_id: int) -> dict | None:
        return User.objects.filter(user_id=user_id).values(
            'user_id', 'email', 'name', 'role', 'access_token', 'is_active'
        ).first()
