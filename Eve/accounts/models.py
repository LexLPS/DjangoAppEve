from django.contrib.auth.models import User
from django.db import models

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_long_term_patient = models.BooleanField(default=False)
    hospital_name = models.CharField(max_length=255, blank=True)
    room_number = models.CharField(max_length=50, blank=True)
    preferred_vr_mode = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"Profile({self.user.username})"