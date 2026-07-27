from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "List privileged accounts (staff/superuser) with last login, active "
        "state, and MFA device count. Run as part of the quarterly access "
        "review (docs/SECURITY_OPERATIONS.md)."
    )

    def handle(self, *args, **options):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        privileged = User.objects.filter(is_staff=True) | User.objects.filter(
            is_superuser=True
        )
        privileged = privileged.distinct().order_by("username")

        if not privileged.exists():
            self.stdout.write("No privileged accounts found.")
            return

        for user in privileged:
            devices = TOTPDevice.objects.filter(user=user, confirmed=True).count()
            flags = []
            if user.is_superuser:
                flags.append("SUPERUSER")
            if user.is_staff:
                flags.append("staff")
            if not user.is_active:
                flags.append("INACTIVE")
            last_login = user.last_login.strftime("%Y-%m-%d") if user.last_login else "never"
            mfa = f"{devices} MFA device(s)" if devices else "NO MFA DEVICE"
            self.stdout.write(
                f"{user.username:<20} [{', '.join(flags)}] "
                f"last login: {last_login:<12} {mfa}"
            )

        self.stdout.write(
            "\nReview: remove privileges no longer needed, deactivate departed "
            "users, and provision MFA (manage.py provision_totp <user>) for any "
            "admin without a device."
        )
