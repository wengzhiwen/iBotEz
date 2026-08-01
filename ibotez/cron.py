"""Minimal 5-field cron matcher (minute hour day-of-month month day-of-week).

Per field supports: ``*``  ``*/N``  ``N``  ``N-M``  ``N,M`` (and combinations
like ``0,30`` or ``9-17``). No month/weekday names, no step-with-range.

``matches(expr, dt)`` returns True if local datetime ``dt`` hits ``expr``.
Standard cron semantics: when BOTH day-of-month and day-of-week are restricted,
the expression fires if *either* matches.
"""
from __future__ import annotations

from datetime import datetime

# (name, min, max) for the five cron fields
_FIELDS = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (cron: 0=Sunday .. 6=Saturday)
)


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
        if base == "*" or base == "":
            b, e = lo, hi
        elif "-" in base:
            a, b2 = base.split("-", 1)
            b, e = int(a), int(b2)
        else:
            b = int(base)
            e = b if "/" not in part else hi
        out.update(range(b, e + 1, step))
    return {x for x in out if lo <= x <= hi}


def matches(expr: str, dt: datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute, hour, dom, mon, dow = (
        _parse_field(fields[i], lo, hi) for i, (lo, hi) in enumerate(_FIELDS)
    )
    # Python weekday(): Mon=0..Sun=6 -> cron dow (0=Sunday) = (weekday()+1)%7
    cron_dow = (dt.weekday() + 1) % 7

    if dt.minute not in minute:
        return False
    if dt.hour not in hour:
        return False
    if dt.month not in mon:
        return False

    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    dom_match = dt.day in dom
    dow_match = cron_dow in dow
    if dom_restricted and dow_restricted:
        return dom_match or dow_match
    return dom_match and dow_match
