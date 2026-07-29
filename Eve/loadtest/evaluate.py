"""Offline release gate for Locust CSV output and resource snapshots.

Locust's in-process gate catches latency and request failures. This evaluator
also proves every required flow ran and that backend pools retained headroom.
It uses only the standard library so it can run in CI or an operator shell.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .config import MAX_FAILURE_RATIO, P95_BUDGETS_MS

MIN_REQUESTS_PER_FLOW = 20
MAX_POOL_UTILIZATION = 0.80


def _number(row, key, default=0.0):
    value = row.get(key, "")
    if value in (None, "", "N/A"):
        return default
    return float(value)


def evaluate_stats(path: Path, required_flows=None, min_requests=MIN_REQUESTS_PER_FLOW):
    required = set(required_flows or P95_BUDGETS_MS)
    failures = []
    seen = set()
    total_requests = total_failures = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Name", "")
            if name in ("Aggregated", ""):
                continue
            requests = int(_number(row, "Request Count"))
            failed = int(_number(row, "Failure Count"))
            total_requests += requests
            total_failures += failed
            if name not in required:
                continue
            seen.add(name)
            if requests < min_requests:
                failures.append(f"{name}: {requests} requests < required {min_requests}")
            p95 = _number(row, "95%")
            budget = P95_BUDGETS_MS.get(name)
            if budget is not None and p95 > budget:
                failures.append(f"{name}: p95 {p95:.0f}ms > {budget}ms")

    for missing in sorted(required - seen):
        failures.append(f"{missing}: required flow missing")
    ratio = total_failures / total_requests if total_requests else 1.0
    if ratio > MAX_FAILURE_RATIO:
        failures.append(f"failure ratio {ratio:.2%} > {MAX_FAILURE_RATIO:.2%}")
    return failures, {"requests": total_requests, "failures": total_failures}


def evaluate_resources(path: Path):
    failures = []
    samples = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                failures.append(f"resource line {line_number}: invalid JSON")
                continue
            if sample.get("event") == "mongo_pool_exhausted":
                failures.append(f"resource line {line_number}: Mongo pool exhausted")
                continue
            if sample.get("event") != "resource_snapshot":
                continue
            samples += 1
            for used_key, max_key in (
                ("pg_total", "pg_max"),
                ("redis_in_use", "redis_max"),
                ("mongo_current", "mongo_max_pool"),
            ):
                used, maximum = sample.get(used_key), sample.get(max_key)
                if isinstance(used, (int, float)) and isinstance(maximum, (int, float)) and maximum:
                    if used / maximum > MAX_POOL_UTILIZATION:
                        failures.append(
                            f"resource line {line_number}: {used_key} utilization "
                            f"{used / maximum:.0%} > {MAX_POOL_UTILIZATION:.0%}"
                        )
            if sample.get("checkout_uncertain", 0):
                failures.append(f"resource line {line_number}: uncertain checkout backlog")
    if not samples:
        failures.append("no resource_snapshot events found")
    return failures, {"resource_samples": samples}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate Eve staging load-test evidence")
    parser.add_argument("--stats", required=True, type=Path, help="Locust *_stats.csv")
    parser.add_argument("--resources", required=True, type=Path, help="JSONL resource snapshots")
    parser.add_argument("--min-requests", type=int, default=MIN_REQUESTS_PER_FLOW)
    args = parser.parse_args(argv)

    failures, summary = evaluate_stats(args.stats, min_requests=args.min_requests)
    resource_failures, resource_summary = evaluate_resources(args.resources)
    failures.extend(resource_failures)
    report = {**summary, **resource_summary, "passed": not failures, "failures": failures}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
