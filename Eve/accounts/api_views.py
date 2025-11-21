from rest_framework import viewsets, permissions
from .models import Profile
from .serializers import ProfileSerializer

class ProfileViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Profile.objects.filter(user=self.request.user)