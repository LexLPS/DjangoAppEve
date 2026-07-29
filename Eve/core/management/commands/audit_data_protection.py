"""Fail-closed audit of backup evidence and recovery-test freshness."""

from datetime import UTC, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class Command(BaseCommand):
    help = "Verify backup freshness, encryption/offsite attestations, and restore-test age."

    def _age_failure(self, name, raw_value, maximum_age, now):
        if not raw_value:
            return f"{name}: evidence missing"
        parsed = parse_datetime(raw_value)
        if parsed is None:
            return f"{name}: invalid ISO-8601 timestamp"
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, UTC)
        age = now - parsed
        if age < timedelta(0):
            return f"{name}: timestamp is in the future"
        if age > maximum_age:
            return f"{name}: evidence is stale ({age.days}d {age.seconds // 3600}h old)"
        return None

    def handle(self, *args, **options):
        now = timezone.now()
        checks = (
            (
                "PostgreSQL backup",
                settings.POSTGRES_BACKUP_LAST_SUCCESS_AT,
                timedelta(hours=settings.BACKUP_MAX_AGE_HOURS),
            ),
            (
                "MongoDB backup",
                settings.MONGODB_BACKUP_LAST_SUCCESS_AT,
                timedelta(hours=settings.BACKUP_MAX_AGE_HOURS),
            ),
            (
                "Restore test",
                settings.RESTORE_TEST_LAST_SUCCESS_AT,
                timedelta(days=settings.RESTORE_TEST_MAX_AGE_DAYS),
            ),
        )
        failures = [failure for name, value, age in checks if (failure := self._age_failure(name, value, age, now))]
        if not settings.BACKUP_ENCRYPTION_CONFIRMED:
            failures.append("Backup encryption: not confirmed")
        if not settings.BACKUP_OFFSITE_CONFIRMED:
            failures.append("Offsite/isolated storage: not confirmed")
        if failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR(failure))
            raise CommandError("Data-protection readiness audit failed")
        self.stdout.write(
            self.style.SUCCESS(
                "Data-protection readiness audit passed: PostgreSQL and MongoDB "
                "backups are fresh, storage controls are attested, and restore testing is current."
            )
        )
