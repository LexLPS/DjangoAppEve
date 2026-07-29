"""Saleor GraphQL client.

Resilience properties:
- One pooled requests.Session per process (connection reuse, capped pool)
- Bounded retries with exponential backoff and jitter — read queries only;
  mutations are never auto-retried because they are not idempotent
- Cache-backed circuit breaker shared across workers: after consecutive
  failures the circuit opens and calls fail fast for a cooldown period
- Error messages carry status codes and metadata only. Response bodies,
  tokens, and personal data must never appear in exceptions or logs.
"""
import logging
import random
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

SALEOR_GRAPHQL_URL = settings.SALEOR_GRAPHQL_URL
SALEOR_CHANNEL = settings.SALEOR_CHANNEL

CONNECT_TIMEOUT = 3.05
READ_TIMEOUT = 10
MAX_ATTEMPTS = 3          # 1 initial + 2 retries, reads only
BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS = {429, 502, 503, 504}

CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 60
_CB_FAILURES_KEY = "saleor:circuit:failures"
_CB_OPEN_KEY = "saleor:circuit:open"
# Outlives the cooldown so the first success afterwards can log recovery
_CB_WAS_OPEN_KEY = "saleor:circuit:was-open"


def _log_call(outcome: str, started: float, attempts: int, **extra):
    """One structured event per upstream call — request rate, outcome mix,
    and upstream latency are all derived from these (docs/OBSERVABILITY.md)."""
    logger.info(
        "saleor call %s in %.0fms (%d attempt(s))",
        outcome, (time.monotonic() - started) * 1000, attempts,
        extra={
            "event": "saleor_call",
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "attempts": attempts,
            **extra,
        },
    )

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=10)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


class SaleorAPIError(RuntimeError):
    """Structured Saleor failure.

    The message is composed exclusively of safe metadata — a machine-readable
    code, the HTTP status, and the content type. Response bodies must never
    be passed into this exception: everything in it may end up in logs and
    tracebacks. `detail` is for safe metadata only (e.g. GraphQL error
    codes), never body excerpts.
    """

    def __init__(self, code: str, status=None, content_type=None, detail=None):
        self.code = code
        self.status = status
        self.content_type = content_type
        parts = [f"saleor_error code={code}"]
        if status is not None:
            parts.append(f"status={status}")
        if content_type is not None:
            parts.append(f"content_type={content_type!r}")
        if detail:
            parts.append(f"detail={detail}")
        super().__init__(" ".join(parts))


class SaleorCircuitOpen(SaleorAPIError):
    """Failing fast: Saleor has been failing repeatedly and is in cooldown."""


# --- Circuit breaker (shared via Django cache / Redis). Cache failures are
# swallowed: an unreachable cache must not take the client down with it.

def _circuit_is_open() -> bool:
    try:
        return cache.get(_CB_OPEN_KEY) is not None
    except Exception:
        return False


def _circuit_record_failure():
    try:
        if cache.add(_CB_FAILURES_KEY, 1, timeout=CIRCUIT_COOLDOWN_SECONDS * 2):
            failures = 1
        else:
            failures = cache.incr(_CB_FAILURES_KEY)
        if failures >= CIRCUIT_FAILURE_THRESHOLD:
            cache.set(_CB_OPEN_KEY, True, timeout=CIRCUIT_COOLDOWN_SECONDS)
            cache.set(_CB_WAS_OPEN_KEY, True, timeout=CIRCUIT_COOLDOWN_SECONDS * 10)
            cache.delete(_CB_FAILURES_KEY)
            logger.error(
                "Saleor circuit opened after %d consecutive failures; "
                "failing fast for %ds", failures, CIRCUIT_COOLDOWN_SECONDS,
                extra={"event": "saleor_circuit", "state": "open",
                       "failures": failures,
                       "cooldown_seconds": CIRCUIT_COOLDOWN_SECONDS},
            )
    except Exception:
        logger.exception("Circuit-breaker cache unavailable")


def _circuit_record_success():
    try:
        cache.delete(_CB_FAILURES_KEY)
        # Pair every "circuit opened" with a recovery event, so an alert on
        # an open circuit can be resolved automatically
        if cache.get(_CB_WAS_OPEN_KEY):
            cache.delete(_CB_WAS_OPEN_KEY)
            logger.warning(
                "Saleor circuit closed: upstream call succeeded again",
                extra={"event": "saleor_circuit", "state": "closed"},
            )
    except Exception:
        pass


def _backoff_sleep(attempt: int):
    delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
    time.sleep(delay)


def _do_request(query: str, variables: dict) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if getattr(settings, "SALEOR_API_TOKEN", ""):
        headers["Authorization"] = f"Bearer {settings.SALEOR_API_TOKEN}"
    return _session.post(
        SALEOR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )


def _parse_response(response: requests.Response) -> dict:
    """Validate and decode; errors carry metadata only, never bodies."""
    status = response.status_code
    content_type = response.headers.get("content-type", "")

    if status >= 400:
        raise SaleorAPIError("http_error", status=status, content_type=content_type)

    if "application/json" not in content_type:
        raise SaleorAPIError("non_json_response", status=status, content_type=content_type)

    try:
        payload = response.json()
    except ValueError:
        raise SaleorAPIError(
            "invalid_json", status=status, content_type=content_type
        ) from None

    if not isinstance(payload, dict):
        raise SaleorAPIError("unexpected_shape", status=status)

    if payload.get("errors"):
        codes = [
            (e.get("extensions") or {}).get("code", "unknown")
            for e in payload["errors"]
            if isinstance(e, dict)
        ]
        raise SaleorAPIError("graphql_error", status=status, detail=f"codes={codes}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise SaleorAPIError("missing_data", status=status)
    return data


def saleor_graphql(query: str, variables: dict, retry: bool = True) -> dict:
    """Execute a GraphQL request and return the `data` payload.

    retry=True is only safe for read queries. Mutations must pass
    retry=False — a timed-out mutation may have been applied.
    """
    if not SALEOR_GRAPHQL_URL:
        raise SaleorAPIError("not_configured")

    started = time.monotonic()

    if _circuit_is_open():
        _log_call("circuit_open", started, 0)
        raise SaleorCircuitOpen("circuit_open")

    attempts = MAX_ATTEMPTS if retry else 1
    last_error = None

    for attempt in range(attempts):
        try:
            response = _do_request(query, variables)
            if retry and response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                logger.warning(
                    "Saleor HTTP %d, retrying (attempt %d/%d)",
                    response.status_code, attempt + 1, attempts,
                )
                _backoff_sleep(attempt)
                continue
            data = _parse_response(response)
        except requests.Timeout:
            last_error = SaleorAPIError("timeout")
            logger.warning("Saleor timeout (attempt %d/%d)", attempt + 1, attempts)
        except requests.RequestException as exc:
            last_error = SaleorAPIError(
                "connection_error", detail=type(exc).__name__
            )
            logger.warning(
                "Saleor connection error %s (attempt %d/%d)",
                type(exc).__name__, attempt + 1, attempts,
            )
        except SaleorAPIError as exc:
            _circuit_record_failure()
            _log_call(exc.code, started, attempt + 1, status=exc.status)
            raise
        else:
            _circuit_record_success()
            _log_call("ok", started, attempt + 1)
            return data

        if attempt < attempts - 1:
            _backoff_sleep(attempt)

    _circuit_record_failure()
    _log_call(last_error.code, started, attempts)
    raise last_error


PRODUCT_FIELDS = """
    id
    name
    slug
    description
    thumbnail {
      url
    }
    defaultVariant {
      id
    }
    pricing {
      priceRange {
        start { gross { amount currency } }
        stop  { gross { amount currency } }
      }
    }
"""


def fetch_products_from_saleor(first=20):
    query = f"""
    query ($first: Int!, $channel: String!) {{
      products(first: $first, channel: $channel) {{
        edges {{
          node {{
            {PRODUCT_FIELDS}
          }}
        }}
      }}
    }}
    """
    data = saleor_graphql(query, {"first": first, "channel": SALEOR_CHANNEL})
    try:
        return [edge["node"] for edge in data["products"]["edges"]]
    except (KeyError, TypeError):
        raise SaleorAPIError("incomplete_response") from None


def fetch_product_by_slug(slug: str):
    query = f"""
    query ($slug: String!, $channel: String!) {{
      product(slug: $slug, channel: $channel) {{
        {PRODUCT_FIELDS}
        media {{
          url
        }}
      }}
    }}
    """
    data = saleor_graphql(query, {"slug": slug, "channel": SALEOR_CHANNEL})
    if "product" not in data:
        raise SaleorAPIError("incomplete_response")
    return data["product"]  # can be None if slug not found
