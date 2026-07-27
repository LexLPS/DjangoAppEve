"""Gunicorn configuration. Tunables come from the environment so the same
image serves every deployment size."""
import multiprocessing
import os

bind = "0.0.0.0:8000"

workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get("GUNICORN_THREADS", "1"))

timeout = 30
graceful_timeout = 30
keepalive = 5

# Recycle workers to bound slow leaks; jitter avoids synchronized restarts
max_requests = 1000
max_requests_jitter = 100

# Only the reverse proxy may set forwarded headers. "*" is never acceptable.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")

accesslog = "-"
errorlog = "-"
