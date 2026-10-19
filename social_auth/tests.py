import json
import time
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from rest_framework.test import APIClient

from home.models import ArtisanProfile, CustomUser, CustomerProfile, StaffSecurityChallenge
from home.otp_security import hash_otp_code, otp_cache_key
from home.security_mail import decrypt_email_code

from .exceptions import SocialAuthError
from .models import SocialIdentity
from .oauth import consume_transaction, create_transaction, exchange_authorization_code, verify_identity_token


TEST_SETTINGS = {
    "ROOT_URLCONF": "social_auth.test_urls",
    "CACHES": {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "social-auth-tests",
        }
    },
    "SOCIAL_AUTH_HTTP_TIMEOUT_SECONDS": 2,
    "SOCIAL_AUTH_GOOGLE_WEB_CLIENT_ID": "google-web-client",
    "SOCIAL_AUTH_GOOGLE_WEB_CLIENT_SECRET": "test-google-web-secret",
    "SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI": "https://market.example/auth/social/google/callback/",
    "SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_ID": "google-native-client",
    "SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_SECRET": "",
    "SOCIAL_AUTH_GOOGLE_NATIVE_REDIRECT_URI": "com.googleusercontent.apps.test:/oauth2redirect",
    "SOCIAL_AUTH_APPLE_WEB_CLIENT_ID": "market.example.service",
    "SOCIAL_AUTH_APPLE_WEB_REDIRECT_URI": "https://market.example/auth/social/apple/callback/",
    "SOCIAL_AUTH_APPLE_NATIVE_CLIENT_ID": "com.example.market",
    "SOCIAL_AUTH_APPLE_TEAM_ID": "TEAMTEST123",
    "SOCIAL_AUTH_APPLE_KEY_ID": "KEYTEST123",
    "SOCIAL_AUTH_APPLE_PRIVATE_KEY": "not-used-in-mocked-tests",
}


@override_settings(**TEST_SETTINGS)
class SocialAuthApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def start_native(self, *, provider="google", intent="register", role="customer"):
        data = {"intent": intent}
        if role:
            data["role"] = role
        return self.client.post(f"/api/v1/social-auth/{provider}/native/start/", data, format="json")

    def start_web(self, *, provider="google", intent="register", role="customer", browser_binding="b" * 43):
        data = {"intent": intent, "browser_binding": browser_binding}
        if role:
            data["role"] = role
        return self.client.post(f"/api/v1/social-auth/{provider}/web/start/", data, format="json")

    def complete_native(self, *, provider="google", state, identity, intent="register", first_name="", last_name=""):
        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity):
            return self.client.post(
                f"/api/v1/social-auth/{provider}/native/complete/",
                {
                    "state": state,
                    "authorization_code": "one-time-authorization-code",
                    "first_name": first_name,
                    "last_name": last_name,
                },
                format="json",
            )

    @staticmethod
    def identity(*, provider="google", email="nia@example.com", subject="provider-subject", first_name="Nia", last_name="Okafor"):
        return {
            "provider": provider,
            "subject": subject,
            "email": email,
            "email_verified": True,
            "first_name": first_name,
            "last_name": last_name,
        }

    def test_native_start_keeps_existing_anonymous_only_auth_boundary(self):
        user = CustomUser.objects.create_user(
            email="signed-in@example.com",
            first_name="Signed",
            last_name="In",
            password="StrongPassword123!",
            is_active=True,
        )
        self.client.force_authenticate(user=user)
        response = self.start_native()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.data)

    def test_native_start_returns_provider_state_nonce_and_google_pkce_without_a_secret(self):
        response = self.start_native()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["state"])
        self.assertTrue(response.data["nonce"])
        self.assertTrue(response.data["code_challenge"])
        self.assertEqual(response.data["code_challenge_method"], "S256")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", response.data["authorization_url"])
        self.assertNotIn("client_secret", response.data)

    def test_web_start_returns_provider_redirect_and_keeps_browser_binding_server_side(self):
        browser_binding = "front-session-binding-0123456789abcdef"
        response = self.start_web(browser_binding=browser_binding)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["provider"], "google")
        self.assertEqual(response.data["intent"], "register")
        self.assertEqual(response.data["expires_in"], 600)
        self.assertIn("authorization_url", response.data)
        self.assertNotIn("browser_binding", response.data)
        self.assertNotIn("client_secret", response.data)
        transaction = cache.get("social_auth:state:" + __import__("hashlib").sha256(response.data["state"].encode()).hexdigest())
        self.assertEqual(transaction["browser_binding"], browser_binding)
        query = parse_qs(urlparse(response.data["authorization_url"]).query)
        self.assertEqual(query["redirect_uri"], [TEST_SETTINGS["SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI"]])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["nonce"][0])

    def test_web_completion_checks_frontend_binding_and_creates_registration_via_api(self):
        binding = "front-session-binding-0123456789abcdef"
        start = self.start_web(browser_binding=binding)
        identity = self.identity()
        payload = {
            "state": start.data["state"],
            "authorization_code": "one-time-authorization-code",
            "browser_binding": binding,
        }
        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ) as send_email:
            response = self.client.post(
                "/api/v1/social-auth/google/web/complete/",
                {**payload, "browser_binding": "wrong-browser-binding-value-123456"},
                format="json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse(CustomUser.objects.exists())
            accepted = self.client.post("/api/v1/social-auth/google/web/complete/", payload, format="json")
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.data["action"], "redirect_to_otp_verification")
        self.assertTrue(CustomUser.objects.filter(email="nia@example.com", is_active=False).exists())
        send_email.assert_called_once()

    def test_web_start_requires_registration_role_and_builds_apple_form_post_redirect(self):
        missing_role = self.client.post(
            "/api/v1/social-auth/apple/web/start/",
            {"intent": "register", "browser_binding": "front-session-binding-0123456789abcdef"},
            format="json",
        )
        self.assertEqual(missing_role.status_code, 400)
        self.assertIn("role", missing_role.data["fields"])

        web_start = self.client.post(
            "/api/v1/social-auth/apple/web/start/",
            {
                "intent": "register",
                "role": "customer",
                "browser_binding": "front-session-binding-0123456789abcdef",
            },
            format="json",
        )
        self.assertEqual(web_start.status_code, 200)
        self.assertEqual(parse_qs(urlparse(web_start.data["authorization_url"]).query)["response_mode"], ["form_post"])

    def test_apple_web_api_uses_first_authorization_names_then_existing_otp(self):
        binding = "front-session-binding-0123456789abcdef"
        start = self.start_web(provider="apple", role="artisan", browser_binding=binding)
        identity = self.identity(provider="apple", email="apple-web@example.com", subject="apple-web-sub", first_name="", last_name="")
        user_data = json.dumps({"name": {"firstName": "Ari", "lastName": "James"}})
        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ) as send_email:
            response = self.client.post(
                "/api/v1/social-auth/apple/web/complete/",
                {
                    "state": start.data["state"],
                    "authorization_code": "apple-one-time-code",
                    "browser_binding": binding,
                    "user": user_data,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        user = CustomUser.objects.get(email="apple-web@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Ari", "James"))
        self.assertTrue(ArtisanProfile.objects.filter(user=user).exists())
        self.assertEqual(send_email.call_count, 1)

    def test_web_login_returns_the_existing_api_jwt_shape_for_a_linked_subject(self):
        user = CustomUser.objects.create_user(
            email="web-login@example.com",
            first_name="Nia",
            last_name="Okafor",
            password=None,
            is_active=True,
        )
        CustomerProfile.objects.create(user=user)
        SocialIdentity.objects.create(user=user, provider="google", subject="provider-subject", email=user.email)
        binding = "front-session-binding-0123456789abcdef"
        start = self.start_web(intent="login", role=None, browser_binding=binding)
        identity = self.identity(email=user.email, subject="provider-subject")
        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity):
            response = self.client.post(
                "/api/v1/social-auth/google/web/complete/",
                {
                    "state": start.data["state"],
                    "authorization_code": "google-one-time-code",
                    "browser_binding": binding,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], user.email)

    def _active_staff_social_identity(self, *, email, subject):
        user = CustomUser.objects.create_user(
            email=email,
            first_name="Ayo",
            last_name="Support",
            password=None,
            is_active=True,
            is_staff=True,
        )
        SocialIdentity.objects.create(user=user, provider="google", subject=subject, email=email)
        return user

    def test_web_staff_social_login_returns_only_a_pending_mfa_challenge(self):
        user = self._active_staff_social_identity(
            email="web-staff@example.com",
            subject="web-staff-provider-subject",
        )
        binding = "staff-web-binding-0123456789abcdef"
        start = self.start_web(intent="login", role=None, browser_binding=binding)
        identity = self.identity(email=user.email, subject="web-staff-provider-subject")

        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity), patch(
            "home.staff_mfa.send_staff_security_code.delay"
        ) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/social-auth/google/web/complete/",
                    {
                        "state": start.data["state"],
                        "authorization_code": "google-web-staff-code",
                        "browser_binding": binding,
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["mfa_required"])
        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        challenge = StaffSecurityChallenge.objects.get(pk=response.data["mfa_challenge_id"])
        self.assertEqual(challenge.user, user)
        self.assertEqual(challenge.purpose, StaffSecurityChallenge.Purpose.LOGIN_MFA)
        self.assertEqual(deliver.call_count, 1)

    def test_native_staff_social_login_returns_only_a_pending_mfa_challenge(self):
        user = self._active_staff_social_identity(
            email="native-staff@example.com",
            subject="native-staff-provider-subject",
        )
        start = self.start_native(intent="login", role=None)
        identity = self.identity(email=user.email, subject="native-staff-provider-subject")

        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity), patch(
            "home.staff_mfa.send_staff_security_code.delay"
        ) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/social-auth/google/native/complete/",
                    {
                        "state": start.data["state"],
                        "authorization_code": "google-native-staff-code",
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["mfa_required"])
        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        challenge = StaffSecurityChallenge.objects.get(pk=response.data["mfa_challenge_id"])
        self.assertEqual(challenge.user, user)
        self.assertEqual(challenge.purpose, StaffSecurityChallenge.Purpose.LOGIN_MFA)
        self.assertEqual(deliver.call_count, 1)

    def test_google_native_code_exchange_uses_pkce_without_a_native_client_secret(self):
        transaction = create_transaction(provider="google", platform="native", intent="login")

        class TokenResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"id_token": "signed-provider-token"}

        with patch("social_auth.oauth.requests.post", return_value=TokenResponse()) as post, patch(
            "social_auth.oauth.verify_identity_token", return_value=self.identity()
        ):
            identity = exchange_authorization_code(
                provider="google",
                platform="native",
                code="one-time-code",
                transaction=transaction,
            )
        self.assertEqual(identity["email"], "nia@example.com")
        data = post.call_args.kwargs["data"]
        self.assertNotIn("client_secret", data)
        self.assertTrue(data["code_verifier"])
        self.assertEqual(data["redirect_uri"], TEST_SETTINGS["SOCIAL_AUTH_GOOGLE_NATIVE_REDIRECT_URI"])

    def test_native_start_requires_role_before_registration(self):
        response = self.client.post(
            "/api/v1/social-auth/apple/native/start/",
            {"intent": "register"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.data["fields"])

    def test_google_registration_creates_inactive_user_with_hashed_cache_otp_and_encrypted_delivery(self):
        start = self.start_native()
        self.assertEqual(start.status_code, 200)
        with patch("social_auth.services.send_verification_email.delay") as send_email, patch(
            "social_auth.services.secrets.randbelow", return_value=234567
        ):
            response = self.complete_native(
                state=start.data["state"],
                identity=self.identity(),
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["action"], "redirect_to_otp_verification")
        user = CustomUser.objects.get(email="nia@example.com")
        self.assertFalse(user.is_active)
        self.assertTrue(CustomerProfile.objects.filter(user=user).exists())
        self.assertFalse(ArtisanProfile.objects.filter(user=user).exists())
        self.assertTrue(SocialIdentity.objects.filter(user=user, provider="google", subject="provider-subject").exists())
        otp = "334567"
        stored = cache.get(otp_cache_key("verification", user.email))
        self.assertEqual(stored, hash_otp_code("verification", user.email, otp))
        self.assertIsNone(cache.get("otp_nia@example.com"))
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[:2], ("Nia", "nia@example.com"))
        self.assertNotEqual(send_email.call_args.args[2], otp)
        self.assertEqual(decrypt_email_code(send_email.call_args.args[2]), otp)

        verified = self.client.post(
            "/api/v1/verify-otp/",
            {"email": "nia@example.com", "otp": otp},
            format="json",
        )
        self.assertEqual(verified.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_missing_provider_names_return_one_time_profile_step_then_create_account(self):
        start = self.start_native(provider="apple", role="artisan")
        identity = self.identity(provider="apple", email="tailor@example.com", subject="apple-sub", first_name="", last_name="")
        response = self.complete_native(provider="apple", state=start.data["state"], identity=identity)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["action"], "collect_profile_details")
        token = response.data["registration_token"]

        with patch("social_auth.services.send_verification_email.delay"):
            completed = self.client.post(
                "/api/v1/social-auth/native/complete-profile/",
                {"registration_token": token, "first_name": "Tola", "last_name": "Adebayo"},
                format="json",
            )
        self.assertEqual(completed.status_code, 201)
        user = CustomUser.objects.get(email="tailor@example.com")
        self.assertFalse(user.is_active)
        self.assertEqual((user.first_name, user.last_name), ("Tola", "Adebayo"))
        self.assertTrue(ArtisanProfile.objects.filter(user=user).exists())
        self.assertTrue(CustomerProfile.objects.filter(user=user).exists())
        self.assertIsNone(cache.get("social_auth:pending_profile:" + __import__("hashlib").sha256(token.encode()).hexdigest()))

    def test_existing_email_is_not_automatically_linked_to_a_new_provider(self):
        CustomUser.objects.create_user(
            email="nia@example.com",
            first_name="Existing",
            last_name="Account",
            password="ExistingStrongPassword123!",
            is_active=True,
        )
        start = self.start_native()
        response = self.complete_native(state=start.data["state"], identity=self.identity())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CustomUser.objects.filter(email="nia@example.com").count(), 1)
        self.assertFalse(SocialIdentity.objects.exists())

    def test_native_state_is_single_use_and_provider_bound(self):
        transaction = create_transaction(provider="google", platform="native", intent="register", role="customer")
        with self.assertRaises(SocialAuthError):
            consume_transaction(state=transaction["state"], provider="apple", platform="native")
        consumed = consume_transaction(state=transaction["state"], provider="google", platform="native")
        self.assertEqual(consumed["role"], "customer")
        with self.assertRaises(SocialAuthError):
            consume_transaction(state=transaction["state"], provider="google", platform="native")

    def test_inactive_social_login_stays_on_the_existing_otp_verification_step(self):
        user = CustomUser.objects.create_user(
            email="pending@example.com",
            first_name="Pending",
            last_name="Member",
            password=None,
            is_active=False,
        )
        CustomerProfile.objects.create(user=user)
        SocialIdentity.objects.create(user=user, provider="google", subject="pending-subject", email=user.email)
        start = self.start_native(intent="login", role=None)
        response = self.complete_native(
            state=start.data["state"],
            identity=self.identity(email="pending@example.com", subject="pending-subject"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["action"], "redirect_to_otp_verification")
        self.assertEqual(response.data["email"], "pending@example.com")
        self.assertNotIn("access", response.data)

    def test_locked_social_login_stays_blocked(self):
        user = CustomUser.objects.create_user(
            email="locked@example.com",
            first_name="Locked",
            last_name="Member",
            password=None,
            is_active=False,
            security_status="LOCKED",
        )
        CustomerProfile.objects.create(user=user)
        SocialIdentity.objects.create(user=user, provider="google", subject="locked-subject", email=user.email)
        start = self.start_native(intent="login", role=None)
        response = self.complete_native(
            state=start.data["state"],
            identity=self.identity(email="locked@example.com", subject="locked-subject"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["action"], "contact_support")
        self.assertNotIn("access", response.data)

    def test_provider_login_returns_token_version_claim_for_linked_active_identity(self):
        user = CustomUser.objects.create_user(
            email="nia@example.com",
            first_name="Nia",
            last_name="Okafor",
            password=None,
            is_active=True,
        )
        CustomerProfile.objects.create(user=user)
        SocialIdentity.objects.create(user=user, provider="google", subject="provider-subject", email=user.email)
        start = self.start_native(intent="login", role=None)
        response = self.complete_native(state=start.data["state"], identity=self.identity())
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        claims = jwt.decode(response.data["access"], options={"verify_signature": False})
        self.assertEqual(claims["token_version"], user.token_version)


class SocialAuthTokenValidationTests(TestCase):
    def test_provider_jwt_signature_audience_issuer_nonce_and_email_are_checked(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
        public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "sub": "google-subject-123",
            "aud": "google-native-client",
            "exp": now + 300,
            "iat": now,
            "email": "Nia@Example.com",
            "email_verified": True,
            "nonce": "server-generated-nonce",
            "given_name": "Nia",
            "family_name": "Okafor",
        }
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
        with patch("social_auth.oauth._fetch_jwks", return_value=[public_jwk]):
            identity = verify_identity_token(
                provider="google",
                id_token=token,
                client_id="google-native-client",
                expected_nonce="server-generated-nonce",
            )
            self.assertEqual(identity["email"], "nia@example.com")
            self.assertEqual(identity["subject"], "google-subject-123")
            invalid_claim_sets = (
                {"iss": "https://attacker.example"},
                {"aud": "another-client"},
                {"email_verified": False},
                {"email": "not-an-email"},
            )
            for changes in invalid_claim_sets:
                with self.subTest(changes=changes), self.assertRaises(SocialAuthError):
                    invalid_token = jwt.encode(
                        {**claims, **changes},
                        private_key,
                        algorithm="RS256",
                        headers={"kid": "test-key"},
                    )
                    verify_identity_token(
                        provider="google",
                        id_token=invalid_token,
                        client_id="google-native-client",
                        expected_nonce="server-generated-nonce",
                    )
            with self.assertRaises(SocialAuthError):
                verify_identity_token(
                    provider="google",
                    id_token=token,
                    client_id="google-native-client",
                    expected_nonce="wrong-nonce",
                )

        apple_claims = {
            **claims,
            "iss": "https://appleid.apple.com",
            "sub": "apple-subject-456",
            "aud": "com.example.market",
            "email": "member@example.com",
        }
        apple_token = jwt.encode(apple_claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
        with patch("social_auth.oauth._fetch_jwks", return_value=[public_jwk]):
            apple_identity = verify_identity_token(
                provider="apple",
                id_token=apple_token,
                client_id="com.example.market",
                expected_nonce="server-generated-nonce",
            )
        self.assertEqual(apple_identity["provider"], "apple")
        self.assertEqual(apple_identity["subject"], "apple-subject-456")


@override_settings(**TEST_SETTINGS)
class SocialAuthWebFlowTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_browser_callback_requires_the_binding_cookie_from_the_starting_browser(self):
        start = self.client.post(
            "/auth/social/google/start/",
            {"intent": "register", "role": "customer", "first_name": "Nia", "last_name": "Okafor"},
        )
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        other_browser = Client()
        rejected = other_browser.get("/auth/social/google/callback/", {"state": state, "code": "code"})
        self.assertEqual(rejected.status_code, 302)
        self.assertFalse(CustomUser.objects.exists())

        identity = SocialAuthApiTests.identity()
        with patch("social_auth.web_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ):
            accepted = self.client.get("/auth/social/google/callback/", {"state": state, "code": "code"})
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(CustomUser.objects.filter(email="nia@example.com").exists())

    def test_apple_binding_cookie_is_secure_and_cross_site_compatible_in_production(self):
        with override_settings(ENVIRONMENT="production"):
            response = self.client.post(
                "/auth/social/apple/start/",
                {"intent": "register", "role": "customer"},
            )
        cookie = response.cookies["artisan_social_auth_browser"]
        self.assertEqual(cookie["samesite"], "None")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])

    def test_google_web_start_uses_state_nonce_pkce_and_registered_callback(self):
        response = self.client.post(
            "/auth/social/google/start/",
            {
                "intent": "register",
                "role": "customer",
                "first_name": "Nia",
                "last_name": "Okafor",
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["redirect_uri"], [TEST_SETTINGS["SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI"]])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])

    def test_google_callback_creates_account_shows_backend_message_and_redirects_to_otp(self):
        start = self.client.post(
            "/auth/social/google/start/",
            {"intent": "register", "role": "customer", "first_name": "Nia", "last_name": "Okafor"},
        )
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        identity = SocialAuthApiTests.identity()
        with patch("social_auth.web_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ):
            response = self.client.get(
                "/auth/social/google/callback/",
                {"state": state, "code": "one-time-authorization-code"},
                follow=True,
            )
        verify_url = urlparse(response.redirect_chain[0][0])
        self.assertEqual(verify_url.path, "/auth/verify-otp/")
        self.assertEqual(parse_qs(verify_url.query), {"email": ["nia@example.com"]})
        self.assertContains(response, "Dear esteem Nia! Your account has been successfully registered.")
        self.assertFalse(CustomUser.objects.get(email="nia@example.com").is_active)

    def test_apple_form_post_uses_state_and_one_time_name_data(self):
        start = self.client.post(
            "/auth/social/apple/start/",
            {"intent": "register", "role": "artisan"},
        )
        self.assertEqual(start.status_code, 302)
        query = parse_qs(urlparse(start["Location"]).query)
        self.assertEqual(query["response_mode"], ["form_post"])
        state = query["state"][0]
        identity = SocialAuthApiTests.identity(provider="apple", email="apple@example.com", subject="apple-sub", first_name="", last_name="")
        apple_user = json.dumps({"name": {"firstName": "Ari", "lastName": "James"}, "email": "apple@example.com"})
        with patch("social_auth.web_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ):
            response = self.client.post(
                "/auth/social/apple/callback/",
                {"state": state, "code": "one-time-authorization-code", "user": apple_user},
                follow=True,
            )
        verify_url = urlparse(response.redirect_chain[0][0])
        self.assertEqual(verify_url.path, "/auth/verify-otp/")
        self.assertEqual(parse_qs(verify_url.query), {"email": ["apple@example.com"]})
        self.assertTrue(ArtisanProfile.objects.filter(user__email="apple@example.com").exists())
        user = CustomUser.objects.get(email="apple@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Ari", "James"))


@override_settings(**{**TEST_SETTINGS, "ROOT_URLCONF": "Artisan_platform.urls", "ARTISAN_API_TRANSPORT": "inprocess"})
class SocialAuthFrontendApiBridgeTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_integrated_registration_page_bridge_uses_api_start_and_complete(self):
        start = self.client.post(
            "/auth/social/google/start/",
            {"intent": "register", "role": "customer", "first_name": "Nia", "last_name": "Okafor"},
        )
        self.assertEqual(start.status_code, 302)
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        self.assertEqual(self.client.session["social_auth_state"], state)

        identity = SocialAuthApiTests.identity()
        with patch("social_auth.api_views.exchange_authorization_code", return_value=identity), patch(
            "social_auth.services.send_verification_email.delay"
        ) as send_email, patch("social_auth.services.secrets.randbelow", return_value=234567):
            response = self.client.get(
                "/auth/social/google/callback/",
                {"state": state, "code": "one-time-authorization-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/verify-otp/?email=nia%40example.com", response["Location"])
        user = CustomUser.objects.get(email="nia@example.com")
        self.assertFalse(user.is_active)
        self.assertTrue(CustomerProfile.objects.filter(user=user).exists())
        otp = "334567"
        stored = cache.get(otp_cache_key("verification", user.email))
        self.assertEqual(stored, hash_otp_code("verification", user.email, otp))
        self.assertIsNone(cache.get("otp_nia@example.com"))
        send_email.assert_called_once()
        self.assertNotEqual(send_email.call_args.args[2], otp)
        self.assertEqual(decrypt_email_code(send_email.call_args.args[2]), otp)
