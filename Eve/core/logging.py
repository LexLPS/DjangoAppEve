"""Structured logging: JSON formatter, correlation IDs, and redaction.

Every request gets a correlation ID (X-Request-ID on the response); every
log record carries it, so one request's records can be joined across
workers. The redaction filter is a safety net — the primary control is that
code never logs credentials, cookies, health data, or payment data in the
first place.
"""
import contextvars
import json
import logging
import re
from datetime import datetime, timezone

request_id_var = contextvars.ContextVar("request_id", default="")

# Fields of a LogRecord that are not user-supplied extras
_STANDARD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    "request_id",
}

_REDACTION_PATTERNS = [
    # Credentials and tokens
    (re.compile(r"(?i)(authorization[=:]\s*)\S+.*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(password[\"']?\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret[\"']?\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key[\"']?\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    # Session/CSRF cookies
    (re.compile(r"(?i)(sessionid\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(csrftoken\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(cookie[=:]\s*)\S+.*"), r"\1[REDACTED]"),
    # Payment-shaped data (PAN-like digit runs)
    (re.compile(r"\b\d{13,19}\b"), "[REDACTED-PAN]"),
    # Legacy upstream body-preview phrasing — bodies never belong in logs
    (re.compile(r"(?i)(body starts with:\s*).*"), r"\1[REDACTED]"),
]


def scrub(text: str) -> str:
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RequestIdFilter(logging.Filter):
    """Stamp every record with the current request's correlation ID."""

    def filter(self, record):
        record.request_id = request_id_var.get() or "-"
        return True


class SensitiveDataFilter(logging.Filter):
    """Scrub credential-shaped content from the rendered message."""

    def filter(self, record):
        rendered = record.getMessage()
        scrubbed = scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        return True


class ConsoleFormatter(logging.Formatter):
    """Human-readable dev formatter. Exception blocks (messages and
    tracebacks) bypass logging filters, so they are scrubbed here."""

    def __init__(self):
        super().__init__(
            "{asctime} {levelname} {name} [{request_id}] {message}", style="{"
        )

    def formatException(self, exc_info):
        return scrub(super().formatException(exc_info))


class JsonFormatter(logging.Formatter):
    """One JSON object per line — machine-parseable for the log platform."""

    def format(self, record):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # Exception text bypasses filters — scrub it at format time
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)
