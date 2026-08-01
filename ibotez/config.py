"""Configuration loading and whitelist persistence (TOML, stdlib only)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .imessage import norm_contact


@dataclass
class Schedule:
    """A cron-driven task: run `prompt` on schedule, send Pi's reply to `to`."""
    cron: str
    prompt: str
    to: str
    name: str = ""


@dataclass
class Config:
    # [poll]
    interval: float = 2.0          # initial / minimum poll interval (seconds)
    max_interval: float = 15.0     # adaptive backoff cap when idle
    backoff_factor: float = 1.5    # multiply interval by this on each empty poll

    # [imessage]
    db_path: str = "~/Library/Messages/chat.db"

    # [pi]
    pi_command: list[str] = field(default_factory=lambda: ["pi", "--mode", "rpc"])
    pi_cwd: str | None = None
    progress_interval: float = 30.0      # seconds between progress iMessages (0 = off)
    no_progress_timeout: float = 120.0   # no Pi event for this long => stalled => retry
    max_retries: int = 2                 # bounded retries on stall/failure
    pi_append_instruction: bool = True    # append the iMessage capability note to Pi's system prompt

    # [bridge]
    allow: list[str] = field(default_factory=list)
    reply_on_error: str = ""

    # [[schedule]]  (cron tasks)
    schedules: list[Schedule] = field(default_factory=list)

    # [health]
    health_check_seconds: float = 5.0   # how often the watchdog evaluates
    stall_seconds: float = 600.0        # non-empty queue + no progress => restart
    max_depth: int = 100                # queue-depth warning threshold

    # [log]
    log_file: str = ""                  # empty = <config_dir>/logs/ibotez.log
    log_level: str = "INFO"             # DEBUG / INFO / WARNING / ERROR

    config_path: Path = field(default_factory=lambda: Path("config.toml"))
    state_path: Path = field(default_factory=lambda: Path("state.json"))

    @property
    def allow_set(self) -> set[str]:
        """Normalized whitelist (last-10-digits for phones, lowercase emails)."""
        return {norm_contact(x) for x in self.allow if x}

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        poll = raw.get("poll", {})
        im = raw.get("imessage", {})
        pi = raw.get("pi", {})
        br = raw.get("bridge", {})
        health = raw.get("health", {})
        log = raw.get("log", {})
        schedules = [
            Schedule(
                cron=str(s["cron"]),
                prompt=str(s["prompt"]),
                to=str(s["to"]),
                name=str(s.get("name", "")),
            )
            for s in raw.get("schedule", [])
            if s.get("cron") and s.get("prompt") and s.get("to")
        ]
        return cls(
            interval=float(poll.get("interval_seconds", 2.0)),
            max_interval=float(poll.get("max_interval_seconds", 15.0)),
            backoff_factor=float(poll.get("backoff_factor", 1.5)),
            db_path=im.get("db_path", "~/Library/Messages/chat.db"),
            pi_command=list(pi.get("command", ["pi", "--mode", "rpc"])),
            pi_cwd=pi.get("cwd"),
            progress_interval=float(pi.get("progress_interval_seconds", 30.0)),
            no_progress_timeout=float(pi.get("no_progress_timeout_seconds", 120.0)),
            max_retries=int(pi.get("max_retries", 2)),
            pi_append_instruction=bool(pi.get("append_instruction", True)),
            allow=[str(x) for x in br.get("allow", [])],
            reply_on_error=br.get("reply_on_error", ""),
            schedules=schedules,
            health_check_seconds=float(health.get("check_seconds", 5.0)),
            stall_seconds=float(health.get("stall_seconds", 600.0)),
            max_depth=int(health.get("max_depth", 100)),
            log_file=str(log.get("file", "")),
            log_level=str(log.get("level", "INFO")).upper(),
            config_path=path,
            state_path=path.parent / "state.json",
        )


# Matches:  [bridge] ... allow = [ ... ]   (the array may span lines)
_ALLOW_RE = re.compile(r"(\[bridge\][^\[]*?allow\s*=\s*)\[[^\]]*\]", re.S)


def write_allow(path: str | Path, allow: list[str]) -> None:
    """Rewrite the `allow = [...]` list under [bridge] in a TOML config file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    replacement = "[" + ", ".join(_toml_str(x) for x in allow) + "]"
    if _ALLOW_RE.search(text):
        text = _ALLOW_RE.sub(lambda m: m.group(1) + replacement, text, count=1)
    else:
        if "[bridge]" not in text:
            text = text.rstrip() + "\n\n[bridge]\n"
        text = text.rstrip() + f"\nallow = {replacement}\n"
    path.write_text(text, encoding="utf-8")


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
