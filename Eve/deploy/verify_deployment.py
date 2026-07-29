"""Post-deploy liveness/readiness gate using only the Python standard library."""

import argparse
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


def _get_json(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "eve-release-verifier/1"})
    context = ssl.create_default_context()
    # The caller rejects every scheme except HTTPS and rejects embedded credentials.
    with urllib.request.urlopen(  # nosec B310
        request, timeout=timeout, context=context
    ) as response:
        return response.status, json.load(response)


def verify(base_url, attempts=12, interval=5, timeout=10):
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("--base-url must be a credential-free https:// origin")
    origin = base_url.rstrip("/")
    last_error = "not attempted"
    consecutive_ready = 0
    for attempt in range(1, attempts + 1):
        try:
            live_status, live = _get_json(f"{origin}/healthz/live/", timeout)
            ready_status, ready = _get_json(f"{origin}/healthz/ready/", timeout)
            healthy = (
                live_status == 200
                and live == {"status": "alive"}
                and ready_status == 200
                and ready.get("status") == "ready"
                and all(value == "ok" for value in ready.get("checks", {}).values())
            )
            if healthy:
                consecutive_ready += 1
                if consecutive_ready >= 3:
                    print(json.dumps({"status": "passed", "attempts": attempt}))
                    return True
            else:
                consecutive_ready = 0
                last_error = "probe returned a degraded or unexpected response"
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            consecutive_ready = 0
            last_error = type(exc).__name__
        if attempt < attempts:
            time.sleep(interval)
    print(json.dumps({"status": "failed", "reason": last_error}))
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify an Eve deployment after rollout")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)
    return 0 if verify(args.base_url, args.attempts, args.interval, args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
