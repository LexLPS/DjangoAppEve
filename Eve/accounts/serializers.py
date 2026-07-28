from django.contrib.auth.models import User
from rest_framework import serializers

from .models import Profile


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]

class ProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Profile
        # Data minimization: hospital_name and room_number are health-adjacent
        # and deliberately NOT exposed through the API — the server-rendered
        # profile page is the only place that shows them.
        fields = ["user", "email_verified", "is_long_term_patient", "preferred_vr_mode"]