#!/usr/bin/env python3
"""Emit bounded, non-actionable summaries from unittest output."""
from __future__ import annotations

import re
import sys
from pathlib import Path

_MAX_LINES = 40
_MAX_NAME = 120
_MAX_OUTPUT = 4096
_TEST_RESULT = re.compile(
    r"^(ERROR|FAIL):\s+[A-Za-z0-9_]{1,120}\s+\(([A-Za-z0-9_.]{1,120})\)$"
)
_RAN = re.compile(r"^Ran\s+([0-9]{1,9})\s+tests?\s+in\s+[0-9]+(?:\.[0-9]+)?s$")
_FAILED = re.compile(r"^FAILED\s*\(([^()]*)\)$")
_COUNT = re.compile(
    r"^(errors|failures|skipped|expected failures|unexpected successes)=(\d{1,9})$"
)


def _parse_counts(value: str) -> str | None:
    fields = []
    for item in value.split(","):
        match = _COUNT.fullmatch(item.strip())
        if not match:
            return None
        fields.append((match.group(1), match.group(2)))
    if not fields:
        return None
    return ", ".join(f"{key}={number}" for key, number in fields)


def summarize(path: Path) -> list[str]:
    failures: list[tuple[str, str]] = []
    ran = None
    failed_counts = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip("\r\n")
            result = _TEST_RESULT.fullmatch(line)
            if result and len(failures) < _MAX_LINES:
                failures.append((result.group(1), result.group(2)[:_MAX_NAME]))
                continue
            result = _RAN.fullmatch(line)
            if result:
                ran = result.group(1)
                continue
            result = _FAILED.fullmatch(line)
            if result:
                failed_counts = _parse_counts(result.group(1))

    output = [f"TEST_SUMMARY: ran={ran or 'unknown'} failures={len(failures)}"]
    output.extend(f"TEST_FAILURE: kind={kind} name={name}" for kind, name in failures)
    if failed_counts:
        output.append(f"TEST_COUNTS: {failed_counts}")
    return output


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        lines = summarize(Path(argv[1]))
    except (OSError, ValueError):
        return 3
    text = "\n".join(lines)[:_MAX_OUTPUT]
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
