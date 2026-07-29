"""Gunicorn configuration. Tunables come from the environment so the same
image serves every deployment size."""
import multiprocessing
import os


def detect_cpus() -> float:
    """CPUs *allocated to this container*, not the host's core count.

    `multiprocessing.cpu_count()` reports the host's CPUs from inside a
    container, so on a small managed instance it can read 8-32 while the
    container is limited to a fraction of a core. Sizing the worker pool
    from that number spawns dozens of processes that contend for one vCPU
    and thrash memory - which looks exactly like application slowness.
    """
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/cpu.max") as handle:
            quota, period = handle.read().split()
        if quota != "max":
            return max(0.5, int(quota) / int(period))
    except (OSError, ValueError):
        pass

    # cgroup v1
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as handle:
            quota = int(handle.read())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as handle:
            period = int(handle.read())
        if quota > 0 and period > 0:
            return max(0.5, quota / period)
    except (OSError, ValueError):
        pass

    return float(multiprocessing.cpu_count())


ALLOCATED_CPUS = detect_cpus()

bind = "0.0.0.0:8000"

# Threads, not just processes: most request time is spent waiting on
# MongoDB, Redis, and Saleor. Sync workers block a whole process during
# that wait; threaded workers keep serving. Password hashing stays
# CPU-bound and is bounded by the process count.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
workers = int(os.environ.get(
    "GUNICORN_WORKERS", max(2, min(int(ALLOCATED_CPUS * 2 + 1), 9))
))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

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


def on_starting(server):
    """Make the effective sizing visible in the deployment logs: guessing it
    from the outside is what hid the oversized pool in the first place."""
    server.log.info(
        "gunicorn sizing: %d workers x %d threads (%s); detected %.2f allocated CPU(s); "
        "PostgreSQL connections per pod <= %d",
        workers, threads, worker_class, ALLOCATED_CPUS, workers * threads,
    )
