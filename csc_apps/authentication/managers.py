from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    """Standard Django custom-user manager, keyed on email instead of username."""

    def create_user(self, email: str, password: str, name: str, role: str, **extra_fields):
        if not email:
            raise ValueError('User must have an email address')
        user = self.model(email=self.normalize_email(email), name=name, role=role, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, name: str = 'Admin', **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'ADMIN')
        return self.create_user(email=email, password=password, name=name, **extra_fields)
