from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Create a TOTP device for a user and print the otpauth:// provisioning "
        "URL and QR code to scan with an authenticator app. Required for admin "
        "logins when ADMIN_REQUIRE_MFA is on."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['username']!r} does not exist.")

        device, created = TOTPDevice.objects.get_or_create(
            user=user, name="default", defaults={"confirmed": True}
        )
        if not created:
            self.stdout.write("Device already exists; showing its provisioning URL.")

        url = device.config_url
        self.stdout.write(url)
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(url)
            qr.print_ascii(invert=True)
        except Exception:
            self.stdout.write("(install `qrcode` to render a scannable QR here)")
