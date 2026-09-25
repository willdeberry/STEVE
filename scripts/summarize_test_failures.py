#!/usr/bin/env python3
"""Emit bounded, non-actionable summaries from unittest output."""
from __future__ import annotations

import re
import sys
from pathlib import Path

_MAX_LINES = 40
_MAX_NAME = 120
_MAX_EXCEPTIONS = 8
_MAX_FRAMES = 12
_MAX_OUTPUT = 4096
_TEST_RESULT = re.compile(
    r"^(ERROR|FAIL):\s+[A-Za-z0-9_]{1,120}\s+\(([A-Za-z0-9_.]{1,120})\)$"
)
_RAN = re.compile(r"^Ran\s+([0-9]{1,9})\s+tests?\s+in\s+[0-9]+(?:\.[0-9]+)?s$")
_FAILED = re.compile(r"^FAILED\s*\(([^()]*)\)$")
_COUNT = re.compile(
    r"^(errors|failures|skipped|expected failures|unexpected successes)=(\d{1,9})$"
)
_EXCEPTION_TYPES = {
    "AssertionError",
    "FileNotFoundError",
    "OSError",
    "PermissionError",
    "RuntimeError",
    "ValueError",
}
_SOURCE_FILES = {
    "test_live_update.py",
    "test_clipboard_bridge.py",
    "update_core.py",
    "live_update.py",
    "STEVEUpdater.py",
}
_FRAME = re.compile(
    r'^\s*File ".*[\\/]([^/\\]+)", line ([0-9]{1,6}), in [A-Za-z_][A-Za-z0-9_]{0,119}$'
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
    exceptions: list[str] = []
    frames: list[tuple[str, str]] = []
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
                continue
            result = _FRAME.fullmatch(line)
            if result and result.group(1) in _SOURCE_FILES and len(frames) < _MAX_FRAMES:
                frame = (result.group(1), result.group(2))
                if frame not in frames:
                    frames.append(frame)
                continue
            if ":" in line:
                exception_name, _message = line.split(":", 1)
                if (exception_name in _EXCEPTION_TYPES
                        and len(exceptions) < _MAX_EXCEPTIONS
                        and exception_name not in exceptions):
                    exceptions.append(exception_name)
                continue

    output = [f"TEST_SUMMARY: ran={ran or 'unknown'} failures={len(failures)}"]
    output.extend(f"TEST_FAILURE: kind={kind} name={name}" for kind, name in failures)
    output.extend(f"TEST_EXCEPTION: type={name}" for name in exceptions)
    output.extend(f"TEST_FRAME: file={file} line={line}"
                  for file, line in frames)
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
    text = "\n".join(lines)
    available = max(0, _MAX_OUTPUT - 1)
    sys.stdout.write(text[:available] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
