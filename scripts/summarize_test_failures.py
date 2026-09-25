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
_MAX_DETAILS = 12
_MAX_FAIL_DETAILS = 12
_MAX_ERROR_DETAILS = 4
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
    details: list[tuple[str, str, str | None, list[tuple[str, str]]]] = []
    current_kind: str | None = None
    current_name: str | None = None
    current_exception: str | None = None
    current_frames: list[tuple[str, str]] = []
    detail_counts = {"FAIL": 0, "ERROR": 0}
    ran = None
    failed_counts = None

    def finish_detail() -> None:
        nonlocal current_kind, current_name, current_exception, current_frames
        if current_kind and current_name is not None and (current_exception or current_frames):
            limit = _MAX_FAIL_DETAILS if current_kind == "FAIL" else _MAX_ERROR_DETAILS
            if detail_counts[current_kind] < limit and len(details) < _MAX_DETAILS:
                details.append((current_kind, current_name, current_exception, current_frames))
                detail_counts[current_kind] += 1
        current_kind = None
        current_name = None
        current_exception = None
        current_frames = []

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip("\r\n")
            result = _TEST_RESULT.fullmatch(line)
            if result:
                finish_detail()
                if len(failures) < _MAX_LINES:
                    failures.append((result.group(1), result.group(2)[:_MAX_NAME]))
                current_kind = result.group(1)
                current_name = result.group(2)[:_MAX_NAME]
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
            if result and result.group(1) in _SOURCE_FILES:
                frame = (result.group(1), result.group(2))
                if current_name is not None and frame not in current_frames and len(current_frames) < 4:
                    current_frames.append(frame)
                if len(frames) < _MAX_FRAMES and frame not in frames:
                    frames.append(frame)
                continue
            if ":" in line:
                exception_name, _message = line.split(":", 1)
                if exception_name in _EXCEPTION_TYPES:
                    if current_name is not None and current_exception is None:
                        current_exception = exception_name
                    if (len(exceptions) < _MAX_EXCEPTIONS
                            and exception_name not in exceptions):
                        exceptions.append(exception_name)
                continue
    finish_detail()

    output = [f"TEST_SUMMARY: ran={ran or 'unknown'} failures={len(failures)}"]
    output.extend(f"TEST_FAILURE: kind={kind} name={name}" for kind, name in failures)
    for kind, name, exception_name, detail_frames in details:
        fields = [f"name={name}"]
        if exception_name:
            fields.append(f"exception={exception_name}")
        if detail_frames:
            fields.append("frames=" + "|".join(f"{file}:{line}" for file, line in detail_frames))
        output.append("TEST_DETAIL: kind=" + kind + " " + " ".join(fields))
    output.extend(f"TEST_EXCEPTION: type={name}" for name in exceptions)
    output.extend(f"TEST_FRAME: file={file} line={line}"
                  for file, line in frames)
    if failed_counts:
        output.append(f"TEST_COUNTS: {failed_counts}")
    return output


def _bounded_output(lines: list[str]) -> str:
    summary = [line for line in lines if line.startswith("TEST_SUMMARY:")]
    failures = [line for line in lines if line.startswith("TEST_FAILURE:")]
    fail_details = [line for line in lines if line.startswith("TEST_DETAIL: kind=FAIL ")]
    other_details = [line for line in lines if line.startswith("TEST_DETAIL:") and line not in fail_details]
    rest = [line for line in lines if line not in summary + failures + fail_details + other_details]
    ordered = summary + fail_details + failures + other_details + rest
    selected: list[str] = []
    used = 1
    for line in ordered:
        cost = len(line) + (1 if selected else 0)
        if used + cost > _MAX_OUTPUT:
            continue
        selected.append(line)
        used += cost
    return "\n".join(selected) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        lines = summarize(Path(argv[1]))
    except (OSError, ValueError):
        return 3
    sys.stdout.write(_bounded_output(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
