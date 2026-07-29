"""Scheduled maintenance tasks."""
from celery import shared_task
from django.core.management import call_command

from .monitoring import log_resource_snapshot


@shared_task
def purge_expired_data():
    call_command("purge_expired_data")


@shared_task
def sample_resources():
    return log_resource_snapshot()
