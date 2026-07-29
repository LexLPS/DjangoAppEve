"""Periodically log connection-pool usage (docs/OBSERVABILITY.md).

Run alongside a load test to see which resource saturates, or on a short
cron in production for a continuous capacity signal:

    python manage.py sample_resources --interval 10 --duration 600
"""
import time

from django.core.management.base import BaseCommand

from core.monitoring import log_resource_snapshot


class Command(BaseCommand):
    help = "Log a PostgreSQL/Redis/MongoDB pool snapshot, once or on an interval."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=0,
                            help="Seconds between samples (0 = single sample)")
        parser.add_argument("--duration", type=int, default=0,
                            help="Stop after this many seconds (0 = run forever)")

    def handle(self, *args, **options):
        interval = options["interval"]
        deadline = time.monotonic() + options["duration"] if options["duration"] else None

        while True:
            stats = log_resource_snapshot()
            self.stdout.write(str(stats))
            if not interval:
                return
            if deadline and time.monotonic() + interval > deadline:
                return
            time.sleep(interval)
