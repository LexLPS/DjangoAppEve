"""Controlled, fail-closed release step for Railway deployments."""

import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run deploy checks and either apply or verify the release schema."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--migrate", action="store_true")
        mode.add_argument("--schema-only", action="store_true")

    def handle(self, *args, **options):
        call_command("check", "--deploy", "--fail-level", "WARNING")
        environment = os.environ.get("DJANGO_ENV", "prod")
        if options["schema_only"]:
            try:
                call_command("migrate", "--check", verbosity=0)
            except SystemExit as exc:
                raise CommandError(
                    "Database schema is not current; deploy the web release first"
                ) from exc
            self.stdout.write(self.style.SUCCESS("Release schema is current."))
            return

        if environment == "prod":
            call_command("audit_data_protection")
        call_command("migrate", "--noinput")
        call_command("ensure_indexes")
        call_command("migrate", "--check", verbosity=0)
        self.stdout.write(self.style.SUCCESS("Controlled release step passed."))
