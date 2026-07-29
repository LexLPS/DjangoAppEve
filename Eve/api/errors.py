"""A single error contract for every API response.

Clients can rely on one shape regardless of what failed:

    {"error": {"code": "...", "message": "...",
               "details": {...}, "request_id": "..."}}

`code` is a stable machine-readable string (clients branch on it),
`message` is human-readable and safe to show, `details` carries field-level
validation errors, and `request_id` ties the failure to the server logs.
"""
import logging

from core.logging import request_id_var
from django.core.exceptions import PermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

# DRF exception -> stable client-facing code
CODES = {
    exceptions.ParseError: "malformed_request",
    exceptions.AuthenticationFailed: "authentication_failed",
    exceptions.NotAuthenticated: "authentication_required",
    exceptions.PermissionDenied: "permission_denied",
    exceptions.NotFound: "not_found",
    exceptions.MethodNotAllowed: "method_not_allowed",
    exceptions.NotAcceptable: "not_acceptable",
    exceptions.UnsupportedMediaType: "unsupported_media_type",
    exceptions.Throttled: "rate_limited",
    exceptions.ValidationError: "validation_error",
}


class APIError(exceptions.APIException):
    """Domain failure with an explicit code, raised by the API views."""

    def __init__(self, code, message, status_code=400, details=None):
        self.code = code
        self.details = details or {}
        self.status_code = status_code
        super().__init__(message)


def _code_for(exc):
    if isinstance(exc, APIError):
        return exc.code
    for exc_type, code in CODES.items():
        if isinstance(exc, exc_type):
            return code
    return "error"


def exception_handler(exc, context):
    """DRF hook: wrap every handled error in the envelope above."""
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)
    if response is None:
        # Unhandled exception: DRF re-raises so Django's 500 handling and
        # Sentry still see it. Never leak the detail to the client.
        return None

    detail = getattr(exc, "detail", None)
    if isinstance(exc, exceptions.ValidationError):
        message = "The request payload failed validation."
        details = detail if isinstance(detail, dict) else {"non_field_errors": detail}
    else:
        message = str(detail) if detail else "Request failed."
        details = getattr(exc, "details", {})

    payload = {
        "error": {
            "code": _code_for(exc),
            "message": message,
            "details": details,
            "request_id": request_id_var.get() or "",
        }
    }
    if isinstance(exc, exceptions.Throttled) and exc.wait:
        payload["error"]["details"] = {"retry_after_seconds": int(exc.wait)}

    response.data = payload
    return response
