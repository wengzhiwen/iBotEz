"""Cron-driven scheduled Pi tasks.

On each minute boundary, every [[schedule]] whose cron expression matches the
current local time is turned into a Job on the shared worker queue (resolved to
the target contact's chat_guid). The worker then runs the prompt and sends the
result, with the same progress/retry handling as incoming messages.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import imessage
from .config import Schedule
from .cron import matches
from .job import Job

log = logging.getLogger("ibotez.scheduler")


async def scheduler(
    schedules: list[Schedule],
    db_path: str,
    queue: asyncio.Queue,
    health,
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
            ))
            health.enqueued += 1
            log.info("scheduled task %r fired -> %s", name, s.to)
