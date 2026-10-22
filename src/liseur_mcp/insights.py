"""Shape checks shared by reading statistics and the live canary."""

from __future__ import annotations

import math
from datetime import date
from typing import Any


def is_number(value: Any) -> bool:
    """Accept JSON numbers, excluding booleans and non-finite decoded floats."""
    # Do not float-convert ints: valid JSON integers can exceed float's range.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def summary_errors(summary: Any) -> list[str]:
    """Return field/type-only diagnostics without changing the upstream data."""
    if not isinstance(summary, dict):
        return [f"GET /v1/insights/summary is an object — {type(summary).__name__}"]
    errors: list[str] = []
    for field in ("total_active_minutes", "total_pages", "speed_prog_per_hour"):
        value = summary.get(field)
        if not is_number(value):
            kind = type(value).__name__ if field in summary else "missing"
            errors.append(f"summary {field} is a number — {kind}")
    for field in ("sessions", "streak_days"):
        value = summary.get(field)
        if type(value) is not int:
            kind = type(value).__name__ if field in summary else "missing"
            errors.append(f"summary {field} is an integer — {kind}")
    return errors


def works_errors(payload: Any) -> list[str]:
    """Check the full works response before any caller applies a row cap."""
    if not isinstance(payload, dict):
        return [f"GET /v1/insights/works is an object — {type(payload).__name__}"]
    works = payload.get("works")
    if not isinstance(works, list):
        return [
            "GET /v1/insights/works carries an object list under works"
            f" — {type(works).__name__}"
        ]
    errors: list[str] = []
    for index, work in enumerate(works):
        if not isinstance(work, dict):
            errors.append(f"works[{index}] is an object — {type(work).__name__}")
            continue
        for field, expected, valid in (
            ("work_id", "a string", isinstance(work.get("work_id"), str)),
            ("sessions", "an integer", type(work.get("sessions")) is int),
            ("total_active_minutes", "a number", is_number(work.get("total_active_minutes"))),
            ("total_pages", "a number", is_number(work.get("total_pages"))),
            ("current_progression", "a number", is_number(work.get("current_progression"))),
            (
                "eta_seconds", "a number or null",
                work.get("eta_seconds") is None or is_number(work.get("eta_seconds")),
            ),
            (
                "last_read_at", "a string or null",
                work.get("last_read_at") is None or isinstance(work.get("last_read_at"), str),
            ),
        ):
            if field not in work or not valid:
                kind = type(work[field]).__name__ if field in work else "missing"
                errors.append(f"works[{index}] {field} is {expected} — {kind}")
        for field in ("title", "author"):
            if field in work and not isinstance(work[field], str):
                errors.append(f"works[{index}] {field} is a string — {type(work[field]).__name__}")
        if "first_read_day" in work and not is_date(work["first_read_day"]):
            errors.append(
                f"works[{index}] first_read_day is a YYYY-MM-DD date"
                f" — {type(work['first_read_day']).__name__}"
            )
    return errors


def is_date(value: Any) -> bool:
    """Require a real, canonical calendar date, not compact or ISO week form."""
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
