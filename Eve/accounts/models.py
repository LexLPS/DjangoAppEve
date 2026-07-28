from django.contrib.auth.models import User
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    email_verified = models.BooleanField(default=False)
    is_long_term_patient = models.BooleanField(default=False)
    hospital_name = models.CharField(max_length=255, blank=True)
    room_number = models.CharField(max_length=50, blank=True)
    preferred_vr_mode = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"Profile({self.user.username})"


class PrivacyActionLog(models.Model):
    """Audit trail for data-subject requests (export / deletion).

    Deliberately not a FK to User — the record must survive the user's
    deletion. Stores identifiers only, never the exported content.
    """

    ACTION_CHOICES = (
        ("export", "Export"),
        ("delete", "Delete"),
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    username = models.CharField(max_length=150)
    user_email = models.CharField(max_length=254, blank=True, default="")
    performed_by = models.CharField(max_length=150)
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.action} {self.username} at {self.created_at:%Y-%m-%d %H:%M}"