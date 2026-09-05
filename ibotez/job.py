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
    builtin: str = ""          # built-in task to run instead of a Pi prompt ("pi-update")
    reply_prefix: str = ""     # prepended to Pi's reply (e.g. "【12】" for session-slot messages)
    retry_attempt: int = 0     # for scheduled tasks: which backoff retry this run is (0 = first)
    commit_date: int = 0       # chat.db date of the source message; the watermark
                               # advances past it only when this job finishes (0 = n/a)

    @property
    def is_scheduled(self) -> bool:
        return self.session_key.startswith("schedule:")

    @property
    def schedule_name(self) -> str:
        """Task name for a scheduled job ("" for interactive jobs)."""
        return self.session_key.removeprefix("schedule:") if self.is_scheduled else ""
