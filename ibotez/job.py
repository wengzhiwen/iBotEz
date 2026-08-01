"""A unified unit of work for the worker queue.

Both incoming iMessages (poller) and scheduled tasks (scheduler) become a Job,
so progress reporting / retry / session handling apply uniformly."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Job:
    session_key: str           # key into state.sessions (normalized contact or "schedule:<name>")
    prompt: str                # text sent to Pi
    reply_to: str              # chat_guid the result is sent to (empty = nowhere)
    label: str                 # human label for logs
    session_name: str | None = None  # optional Pi session display name
