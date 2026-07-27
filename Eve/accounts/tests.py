import re

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .models import Profile


class AuthenticationTests(TestCase):
    def setUp(self):
        cache.clear()  # reset rate-limit counters between tests
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def test_profile_requires_login(self):
        response = self.client.get(reverse("profile"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('profile')}")

    def test_cart_requires_login(self):
        response = self.client.get(reverse("cart"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_api_requires_authentication(self):
        response = self.client.get("/api/profile/")
        self.assertIn(response.status_code, (401, 403))

    def test_login_rejects_wrong_password(self):
        response = self.client.post(
            reverse("login"), {"username": "alice", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)  # re-renders form, no session
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_throttled_after_repeated_attempts(self):
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "alice", "password": "wrong"})
        response = self.client.post(
            reverse("login"), {"username": "alice", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 429)

    def test_registration_rejects_password_mismatch(self):
        response = self.client.post(reverse("register"), {
            "username": "mallory",
            "email": "mallory@example.com",
            "password1": "S3curePass!x",
            "password2": "different",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="mallory").exists())

    def test_registration_rejects_invalid_email(self):
        response = self.client.post(reverse("register"), {
            "username": "mallory",
            "email": "not-an-email",
            "password1": "S3curePass!x",
            "password2": "S3curePass!x",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="mallory").exists())

    def test_registration_throttled_after_repeated_attempts(self):
        for _ in range(5):
            self.client.post(reverse("register"), {})
        response = self.client.post(reverse("register"), {})
        self.assertEqual(response.status_code, 429)


class LogoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def test_logout_get_rejected(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)  # still logged in

    def test_logout_post_ends_session(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class AccountLockoutTests(TestCase):
    """Per-username lockout: credential stuffing across many IPs still locks
    the targeted account, independently of the per-IP throttle."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def _fail_from(self, ip):
        return self.client.post(
            reverse("login"),
            {"username": "alice", "password": "wrong"},
            REMOTE_ADDR=ip,
        )

    def test_lockout_after_distributed_failures(self):
        # 10 failures from 10 different IPs — per-IP throttle never trips
        for i in range(10):
            response = self._fail_from(f"198.51.100.{i}")
            self.assertEqual(response.status_code, 200)
        # Even the CORRECT password from a fresh IP is now refused
        response = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "S3curePass!x"},
            REMOTE_ADDR="203.0.113.99",
        )
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_successful_login_clears_failure_count(self):
        for i in range(5):
            self._fail_from(f"198.51.100.{i}")
        response = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "S3curePass!x"},
            REMOTE_ADDR="203.0.113.1",
        )
        self.assertEqual(response.status_code, 302)  # logged in
        self.client.post(reverse("logout"))
        # Counter was reset — five more failures don't lock yet
        for i in range(5):
            response = self._fail_from(f"198.51.100.{100 + i}")
            self.assertEqual(response.status_code, 200)


class EmailVerificationTests(TestCase):
    def setUp(self):
        cache.clear()

    def _register(self):
        return self.client.post(reverse("register"), {
            "username": "carol",
            "email": "carol@example.com",
            "password1": "S3curePass!x",
            "password2": "S3curePass!x",
        })

    def test_registration_sends_verification_email(self):
        self._register()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("carol@example.com", mail.outbox[0].to)
        self.assertIn("/accounts/verify-email/", mail.outbox[0].body)

    def test_verification_link_marks_email_verified(self):
        self._register()
        match = re.search(r"(/accounts/verify-email/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        response = self.client.get(match.group(1))
        self.assertEqual(response.status_code, 200)
        profile = Profile.objects.get(user__username="carol")
        self.assertTrue(profile.email_verified)

    def test_tampered_token_rejected(self):
        self._register()
        response = self.client.get("/accounts/verify-email/forged-token/")
        self.assertEqual(response.status_code, 400)
        profile = Profile.objects.get(user__username="carol")
        self.assertFalse(profile.email_verified)

    def test_resend_requires_post_and_sends(self):
        self._register()
        mail.outbox.clear()
        response = self.client.get(reverse("resend_verification"))
        self.assertEqual(response.status_code, 405)
        response = self.client.post(reverse("resend_verification"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)


class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def test_reset_email_sent_for_known_address(self):
        response = self.client.post(reverse("password_reset"),
                                    {"email": "alice@example.com"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/reset/", mail.outbox[0].body)

    def test_no_account_enumeration_for_unknown_address(self):
        response = self.client.post(reverse("password_reset"),
                                    {"email": "ghost@example.com"})
        self.assertEqual(response.status_code, 302)  # same outcome either way
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_form_is_rate_limited(self):
        for _ in range(5):
            self.client.post(reverse("password_reset"), {"email": "alice@example.com"})
        response = self.client.post(reverse("password_reset"),
                                    {"email": "alice@example.com"})
        self.assertEqual(response.status_code, 429)


class ObjectOwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "S3curePass!x")
        self.alice_profile = Profile.objects.create(user=self.alice, hospital_name="St. Mary")
        self.bob_profile = Profile.objects.create(user=self.bob, hospital_name="Charité")

    def test_api_lists_only_own_profile(self):
        self.client.force_login(self.alice)
        response = self.client.get("/api/profile/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["user"]["username"], "alice")
        self.assertNotContains(response, "Charité")

    def test_api_denies_access_to_other_users_profile(self):
        self.client.force_login(self.alice)
        response = self.client.get(f"/api/profile/{self.bob_profile.pk}/")
        self.assertEqual(response.status_code, 404)
