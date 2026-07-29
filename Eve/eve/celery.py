"""Celery application for background and scheduled work."""
import logging
import os
import time

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "eve.settings")

app = Celery("eve")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

logger = logging.getLogger("eve.tasks")
_started = {}


@task_prerun.connect
def log_task_started(task_id=None, task=None, **kwargs):
    _started[task_id] = time.monotonic()
    logger.info(
        "Task started: %s",
        task.name,
        extra={"event": "task_started", "task": task.name, "task_id": task_id},
    )


@task_postrun.connect
def log_task_finished(task_id=None, task=None, state=None, **kwargs):
    started = _started.pop(task_id, None)
    duration_ms = round((time.monotonic() - started) * 1000, 1) if started else None
    logger.info(
        "Task finished: %s (%s)",
        task.name,
        state,
        extra={
            "event": "task_finished",
            "task": task.name,
            "task_id": task_id,
            "state": state,
            "duration_ms": duration_ms,
        },
    )


@task_failure.connect
def log_task_failed(task_id=None, sender=None, exception=None, **kwargs):
    logger.error(
        "Task failed: %s (%s)",
        sender.name,
        type(exception).__name__,
        extra={
            "event": "task_failed",
            "task": sender.name,
            "task_id": task_id,
            "error": type(exception).__name__,
        },
    )
