from django.test import RequestFactory, TestCase
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request
from rest_framework.test import APIClient

from csc_apps.authentication.authentication import JWTAuthentication
from csc_apps.authentication.models import User
from csc_apps.authentication.views import AuthView


class LoginTests(TestCase):
    """/auth/login/ must authenticate by email+password, issue a JWT, and store it on
    the user row so a subsequent login invalidates the previous token
    (docs/security-baseline.md §1)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='officer@example.com', password='correct-horse', name='Officer One', role='OFFICER'
        )

    def _client(self):
        return APIClient()

    def test_login_with_correct_credentials_returns_token(self):
        response = self._client().post(
            '/auth/login/', {'email': 'officer@example.com', 'password': 'correct-horse'}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['status'])
        self.assertIn('token', response.data['data'])
        self.assertEqual(response.data['data']['user']['role'], 'OFFICER')

    def test_login_with_wrong_password_is_rejected(self):
        response = self._client().post(
            '/auth/login/', {'email': 'officer@example.com', 'password': 'wrong-password'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data['status'])

    def test_login_persists_token_and_invalidates_previous_session(self):
        client = self._client()
        first = client.post('/auth/login/', {'email': 'officer@example.com', 'password': 'correct-horse'})
        first_token = first.data['data']['token']

        second = client.post('/auth/login/', {'email': 'officer@example.com', 'password': 'correct-horse'})
        second_token = second.data['data']['token']

        self.user.refresh_from_db()
        self.assertEqual(self.user.access_token, second_token)
        self.assertNotEqual(first_token, second_token)


class JWTAuthenticationTests(TestCase):
    """Unit tests for the authentication class itself (docs/pms-reference-analysis.md
    §10 - AI provider interfaces and authentication classes are the two things Phase 0
    testing explicitly targets beyond the happy-path endpoint tests above)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='reviewer@example.com', password='pw', name='Reviewer One', role='REVIEWER'
        )
        self.valid_token = AuthView()._issue_token(self.user)
        User.objects.filter(user_id=self.user.user_id).update(access_token=self.valid_token)

    def _request_with_bearer(self, token: str) -> Request:
        django_request = RequestFactory().get('/any-protected-path/')
        django_request.META['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return Request(django_request)

    def test_valid_current_token_authenticates(self):
        user, payload = JWTAuthentication().authenticate(self._request_with_bearer(self.valid_token))
        self.assertEqual(user.user_id, self.user.user_id)
        self.assertEqual(payload['role'], 'REVIEWER')

    def test_superseded_token_is_rejected(self):
        stale_token = self.valid_token
        # A second login overwrites access_token - docs/security-baseline.md §1.
        new_token = AuthView()._issue_token(self.user)
        User.objects.filter(user_id=self.user.user_id).update(access_token=new_token)

        with self.assertRaises(AuthenticationFailed):
            JWTAuthentication().authenticate(self._request_with_bearer(stale_token))

    def test_missing_bearer_header_returns_none(self):
        django_request = RequestFactory().get('/any-protected-path/')
        self.assertIsNone(JWTAuthentication().authenticate(Request(django_request)))


class Phase9AuthenticationHardeningTests(TestCase):
    """docs/phase9-security-audit-observability.md §Authentication - regression
    coverage for token/identity edge cases beyond Phase 0's happy-path tests."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='officer9@example.com', password='pw', name='Officer Nine', role='OFFICER'
        )

    def test_missing_token_is_rejected(self):
        response = APIClient().get('/recordings/')
        self.assertEqual(response.status_code, 401)

    def test_malformed_token_is_rejected(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-jwt')
        response = client.get('/recordings/')
        self.assertEqual(response.status_code, 403)

    def test_invalid_signature_token_is_rejected(self):
        import jwt as pyjwt

        forged = pyjwt.encode({'user_id': self.user.user_id, 'role': 'ADMIN'}, 'wrong-secret', algorithm='HS256')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {forged}')
        response = client.get('/recordings/')
        self.assertEqual(response.status_code, 403)

    def test_token_cannot_impersonate_another_user_via_edited_payload(self):
        # A token re-signed for a different user_id is rejected the same way a
        # forged signature is - JWTAuthentication verifies the signature against
        # settings.SECRET_KEY, which the client never has.
        other = User.objects.create_user(email='other9@example.com', password='pw', name='Other', role='ADMIN')
        import jwt as pyjwt

        forged = pyjwt.encode({'user_id': other.user_id, 'role': 'ADMIN'}, 'attacker-guessed-secret', algorithm='HS256')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {forged}')
        response = client.get('/recordings/')
        self.assertEqual(response.status_code, 403)

    def test_authenticated_identity_is_resolved_server_side_not_from_request_body(self):
        # request.params.user_id is set by SerializerValidations from the
        # authenticated request.user, never from client-supplied JSON - a body
        # claiming a different userId must not change who the action is recorded
        # against.
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get('/recordings/', {'userId': 999999, 'user_id': 999999})
        self.assertEqual(response.status_code, 200)

    def test_login_produces_an_activity_log_entry(self):
        from csc_apps.activity_log.models import ActivityLog

        APIClient().post('/auth/login/', {'email': 'officer9@example.com', 'password': 'pw'})
        log = ActivityLog.objects.filter(user=self.user, action='Read', details__event='login').first()
        self.assertIsNotNone(log)

    def test_login_audit_never_contains_password_or_token(self):
        from csc_apps.activity_log.models import ActivityLog

        response = APIClient().post('/auth/login/', {'email': 'officer9@example.com', 'password': 'pw'})
        token = response.data['data']['token']
        log = ActivityLog.objects.get(user=self.user, action='Read')
        rendered = str(log.details)
        self.assertNotIn('pw', rendered)
        self.assertNotIn(token, rendered)

    def test_failed_login_does_not_create_activity_log_entry(self):
        # No failed-login-attempt tracking was built in Phase 9 (deliberate scope
        # boundary - docs/phase9-security-audit-observability.md §Remaining risks) -
        # only successful, identity-attributable logins are audited.
        from csc_apps.activity_log.models import ActivityLog

        APIClient().post('/auth/login/', {'email': 'officer9@example.com', 'password': 'wrong'})
        self.assertFalse(ActivityLog.objects.filter(details__event='login').exists())
