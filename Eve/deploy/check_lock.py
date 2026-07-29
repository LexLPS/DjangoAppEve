"""Reject mutable or incomplete Python dependency locks."""

import argparse
import re
from pathlib import Path

PIN = re.compile(r"^[A-Za-z0-9_.-]+==[^\s;]+(?:\s*;.*)?$")


def validate(path):
    failures = []
    packages = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not PIN.fullmatch(line):
            failures.append(f"line {number}: dependency is not exactly pinned")
            continue
        package = line.split("==", 1)[0].lower().replace("_", "-")
        if package in packages:
            failures.append(f"line {number}: duplicate package {package}")
        packages.add(package)
    if not packages:
        failures.append("lock file has no dependencies")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path("requirements.lock"))
    args = parser.parse_args(argv)
    failures = validate(args.path)
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
