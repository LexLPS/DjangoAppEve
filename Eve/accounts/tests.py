import re

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase
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

    def test_logout_post_without_csrf_token_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(reverse("logout"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("_auth_user_id", csrf_client.session)  # still logged in


class SessionFixationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def test_session_key_rotates_on_login(self):
        # Establish a pre-login session an attacker could have planted
        session = self.client.session
        session["planted"] = True
        session.save()
        key_before = session.session_key

        response = self.client.post(
            reverse("login"), {"username": "alice", "password": "S3curePass!x"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.client.session.session_key, key_before)


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

    def test_lockout_notifies_account_owner_once(self):
        for i in range(10):
            self._fail_from(f"198.51.100.{i}")
        lockout_mails = [m for m in mail.outbox if "locked" in m.subject]
        self.assertEqual(len(lockout_mails), 1)
        self.assertIn("alice@example.com", lockout_mails[0].to)
        # Further failures inside the same window don't spam the owner
        self._fail_from("198.51.100.200")
        self.assertEqual(
            len([m for m in mail.outbox if "locked" in m.subject]), 1)

    def test_lockout_of_unknown_username_sends_nothing(self):
        for i in range(10):
            self.client.post(
                reverse("login"),
                {"username": "ghost-user", "password": "wrong"},
                REMOTE_ADDR=f"198.51.100.{i}",
            )
        self.assertEqual(len(mail.outbox), 0)

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
        data = response.json()  # paginated envelope
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["user"]["username"], "alice")
        self.assertNotContains(response, "Charité")

    def test_api_denies_access_to_other_users_profile(self):
        self.client.force_login(self.alice)
        response = self.client.get(f"/api/profile/{self.bob_profile.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_api_rejects_write_methods(self):
        self.client.force_login(self.alice)
        response = self.client.post("/api/profile/", {"hospital_name": "X"})
        self.assertEqual(response.status_code, 405)  # read-only viewset
        response = self.client.delete(f"/api/profile/{self.alice_profile.pk}/")
        self.assertEqual(response.status_code, 405)

    def test_api_never_exposes_health_adjacent_fields(self):
        # Data minimization: hospital and room stay out of the API entirely
        self.client.force_login(self.alice)
        payload = self.client.get("/api/profile/").json()["results"][0]
        self.assertNotIn("hospital_name", payload)
        self.assertNotIn("room_number", payload)


class PrivacyWorkflowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        Profile.objects.create(user=self.user, hospital_name="St. Mary")

    def test_export_user_outputs_data_and_writes_audit_log(self):
        from io import StringIO
        from unittest.mock import patch

        from django.core.management import call_command

        from .models import PrivacyActionLog

        with patch("ecommerce.services.mongo_client.carts_collection") as carts:
            carts.find_one.return_value = None
            out = StringIO()
            call_command("export_user", "alice", "--performed-by", "dpo", stdout=out)

        exported = out.getvalue()
        self.assertIn("alice@example.com", exported)
        self.assertIn("St. Mary", exported)
        log = PrivacyActionLog.objects.get()
        self.assertEqual(log.action, "export")
        self.assertEqual(log.username, "alice")
        self.assertEqual(log.performed_by, "dpo")

    def test_purge_user_deletes_everywhere_and_writes_audit_log(self):
        from io import StringIO
        from unittest.mock import MagicMock, patch

        from django.core.management import call_command

        from .models import PrivacyActionLog

        with patch("ecommerce.services.mongo_client.carts_collection") as carts:
            carts.delete_many.return_value = MagicMock(deleted_count=1)
            call_command("purge_user", "alice", "--yes",
                         "--performed-by", "dpo", stdout=StringIO())

        self.assertFalse(User.objects.filter(username="alice").exists())
        carts.delete_many.assert_called_once_with({"user_id": self.user.id})
        log = PrivacyActionLog.objects.get()
        self.assertEqual(log.action, "delete")
        self.assertEqual(log.performed_by, "dpo")

    def test_purge_user_refuses_without_confirmation(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("purge_user", "alice", "--performed-by", "dpo")
        self.assertTrue(User.objects.filter(username="alice").exists())
