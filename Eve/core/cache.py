"""JSON serializer for the Redis cache backend.

Django's default Redis serializer pickles values, which turns a Redis
compromise into code execution in the app (threat model R2). Everything Eve
caches is JSON-shaped (counters, flags, JWKS documents, session dicts), so
JSON round-trips losslessly and deserialization is inert.

Integers pass through raw — exactly like Django's own serializer — so that
Redis INCR keeps working for rate-limit and lockout counters.
"""
import json


class SafeJSONSerializer:
    def dumps(self, obj):
        if type(obj) is int:  # noqa: E721 — bool must NOT pass through raw
            return obj
        return json.dumps(obj).encode()

    def loads(self, data):
        try:
            return int(data)
        except (ValueError, TypeError):
            return json.loads(data)
