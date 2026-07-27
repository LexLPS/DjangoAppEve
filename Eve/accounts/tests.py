from django.contrib.auth.models import User
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
