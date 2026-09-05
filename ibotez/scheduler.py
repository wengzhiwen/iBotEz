"""Cron-driven scheduled Pi tasks.

On each minute boundary, every [[schedule]] whose cron expression matches the
current local time is turned into a Job on the shared worker queue (resolved to
the target contact's chat_guid). The worker then runs the prompt and sends the
result, with the same progress/retry handling as incoming messages. A schedule
with `builtin` set (e.g. "pi-update") runs that built-in task instead of a Pi
prompt.

SchedState persists per-task bookkeeping (e.g. time of last successful run) in
its own JSON file next to state.json, separate so the running bridge's frequent
state.json rewrites never clobber it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import imessage
from .config import Schedule
from .cron import matches
from .job import Job

log = logging.getLogger("ibotez.scheduler")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # hand-edited naive stamp: assume local time
        dt = dt.astimezone()
    return dt


def _last_due_time(expr: str, now: datetime, window_hours: float) -> datetime | None:
    """Most recent local minute within the window that matches the cron expr."""
    dt = now.replace(second=0, microsecond=0)
    for _ in range(int(window_hours * 60)):
        try:
            if matches(expr, dt):
                return dt
        except ValueError:
            return None  # invalid expression; the main loop logs it
        dt -= timedelta(minutes=1)
    return None


async def _catch_up_missed(
    schedules: list[Schedule],
    db_path: str,
    queue: asyncio.Queue,
    health,
    sched_state: SchedState,
    catchup_hours: float,
) -> None:
    """Fire (once) any scheduled task whose most recent due time was missed.

    Covers a fire missed while the Mac slept / the bridge was down, and a
    failure whose retry chain was interrupted by a restart. A task is only
    caught up when it has neither succeeded nor definitively given up since
    that due time — so restart loops can't spam re-runs.
    """
    now = datetime.now().astimezone()
    for s in schedules:
        if s.builtin:
            continue  # maintenance tasks (e.g. pi-update) re-check on their own
        name = s.name or f"{s.cron}->{s.to}"
        due = _last_due_time(s.cron, now, catchup_hours)
        if due is None:
            continue
        last_ok = _parse_iso(sched_state.last_success.get(name))
        gave_up = _parse_iso(sched_state.last_giveup.get(name))
        if (last_ok is not None and last_ok >= due) or (gave_up is not None and gave_up >= due):
            continue
        chat_guid = imessage.chat_guid_for(db_path, s.to)
        if not chat_guid:
            log.warning(
                "catch-up: task %r cannot resolve %r to a chat_guid — skipping", name, s.to
            )
            continue
        log.info("catching up missed scheduled task %r (due %s)", name, due.strftime("%m-%d %H:%M"))
        await queue.put(Job(
            session_key=f"schedule:{name}",
            prompt=s.prompt,
            reply_to=chat_guid,
            label=f"schedule:{name}(catch-up)",
            session_name=name,
        ))
        health.enqueued += 1


class SchedState:
    """Persistent scheduler bookkeeping (task name -> ISO timestamp)."""

    def __init__(
        self,
        last_success: dict[str, str] | None = None,
        last_giveup: dict[str, str] | None = None,
        path: Path | None = None,
    ):
        self.last_success = last_success or {}
        self.last_giveup = last_giveup or {}
        self.path = path

    @classmethod
    def load(cls, path: Path) -> "SchedState":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(
                    {str(k): str(v) for k, v in dict(data.get("last_success", {})).items()},
                    {str(k): str(v) for k, v in dict(data.get("last_giveup", {})).items()},
                    path,
                )
            except Exception:
                log.exception("sched state file %s unreadable — starting fresh", path)
        return cls(path=path)

    def save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"last_success": self.last_success, "last_giveup": self.last_giveup},
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)


async def scheduler(
    schedules: list[Schedule],
    db_path: str,
    queue: asyncio.Queue,
    health,
    sched_state: SchedState | None = None,
    catchup_hours: float = 24.0,
) -> None:
    # Validate cron expressions up front; drop invalid tasks (with a clear log).
    valid: list[Schedule] = []
    for s in schedules:
        try:
            matches(s.cron, datetime.now())  # raises ValueError if malformed
            valid.append(s)
        except ValueError as e:
            log.error("schedule %r has invalid cron %r: %s — disabled", s.name, s.cron, e)
    schedules = valid

    if not schedules:
        log.info("no scheduled tasks configured")
        return

    if sched_state is not None:
        try:
            await _catch_up_missed(schedules, db_path, queue, health, sched_state, catchup_hours)
        except Exception:
            log.exception("catch-up pass failed — continuing with normal scheduling")

    log.info("scheduler started: %d task(s)", len(schedules))
    while True:
        # Sleep until just past the next minute boundary, then evaluate.
        now = time.time()
        await asyncio.sleep(60 - (now % 60) + 0.2)
        dt = datetime.now()  # local time

        for s in schedules:
            try:
                if not matches(s.cron, dt):
                    continue
            except ValueError as e:
                log.error("schedule %r has bad cron %r: %s", s.name, s.cron, e)
                continue

            chat_guid = imessage.chat_guid_for(db_path, s.to)
            if not chat_guid:
                log.warning("schedule %r: cannot resolve %r to a chat_guid — skipping", s.name, s.to)
                continue

            name = s.name or f"{s.cron}->{s.to}"
            await queue.put(Job(
                session_key=f"schedule:{name}",
                prompt=s.prompt,
                reply_to=chat_guid,
                label=f"schedule:{name}",
                session_name=name,
                builtin=s.builtin,
            ))
            health.enqueued += 1
            log.info("scheduled task %r fired -> %s", name, s.to)
