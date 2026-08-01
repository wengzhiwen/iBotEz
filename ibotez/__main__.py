"""iBotEz CLI.

Commands:
  run    start the bridge (poll -> Pi -> reply)
  chats  list iMessage conversations and manage the whitelist
  send   send a one-off test message to a chat
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, imessage
from .bridge import run as run_bridge
from .config import Config, write_allow

# Dump all-thread tracebacks to stderr on SIGUSR1 — handy for a stuck daemon.
faulthandler.enable()
faulthandler.register(signal.SIGUSR1, all_threads=True)


def _setup_logging(cfg: Config, verbose: bool) -> None:
    """Configure logging to stderr + a file (configurable via [log])."""
    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level or "INFO", logging.INFO)
    root = Path(__file__).resolve().parent.parent
    log_file = cfg.log_file or str(root / "logs" / "ibotez.log")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except Exception:
        pass  # file logging is best-effort; stderr always works
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _fmt_date(date_val: int) -> str:
    if not date_val:
        return ""
    ms = imessage.date_to_unix_ms(date_val)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M")


def _cmd_run(cfg: Config) -> int:
    try:
        asyncio.run(run_bridge(cfg))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def _cmd_chats(cfg: Config) -> int:
    con = imessage.connect(cfg.db_path)
    try:
        chats = imessage.list_chats(con)
    finally:
        con.close()

    if not chats:
        print("No conversations found in chat.db.")
        return 0

    allow_norm = cfg.allow_set
    print(f"{'#':>3}  {'phone / email':<26} {'name':<18} last message")
    print("-" * 84)
    for i, c in enumerate(chats):
        if len(c.handles) == 1:
            handle = c.handles[0]
        elif c.handles:
            handle = ", ".join(c.handles)
        else:
            handle = "—"
        name = (c.display_name or c.identifier or "")[:18]
        when = _fmt_date(c.last_date)
        snippet = ((c.last_text or "").replace("\n", " "))[:30]
        tag = "  (whitelisted)" if imessage.norm_contact(handle) in allow_norm else ""
        print(f"{i:>3}  {str(handle):<26} {name:<18} {when}  {snippet}{tag}")

    sel = input(
        "\nEnter numbers to add to the whitelist (comma-separated), or Enter to skip: "
    ).strip()
    if not sel:
        return 0

    allow = list(cfg.allow)
    existing = {imessage.norm_contact(x) for x in allow}
    added: list[str] = []
    for tok in sel.split(","):
        tok = tok.strip()
        if tok.isdigit():
            idx = int(tok)
            if 0 <= idx < len(chats) and chats[idx].handles:
                h = chats[idx].handles[0]
                if imessage.norm_contact(h) not in existing:
                    allow.append(h)
                    existing.add(imessage.norm_contact(h))
                    added.append(h)
            else:
                print(f"  ignored (out of range or no number): {tok}")
        else:
            print(f"  ignored: {tok!r}")

    if added:
        write_allow(cfg.config_path, allow)
        print(f"\nAdded {len(added)}: {added}")
        print(f"whitelist now has {len(allow)} entry/entries (saved to {cfg.config_path}).")
    else:
        print("Nothing added.")
    return 0


def _cmd_send(chat_guid: str, text: str) -> int:
    imessage.send(chat_guid, text)
    print("sent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ibotez",
        description="iBotEz — a minimal iMessage <-> Pi (pi.dev) bridge for macOS.",
        epilog="Docs: https://github.com/wengzhiwen/iBotEz",
    )
    p.add_argument("-c", "--config", default="config.toml", help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose (DEBUG) logging")
    p.add_argument("-V", "--version", action="version", version=f"ibotez {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    sub.add_parser("run", help="start the bridge")
    sub.add_parser("chats", help="list conversations and manage the whitelist")
    s = sub.add_parser("send", help="send a one-off test message")
    s.add_argument("chat_guid", help="chat GUID, e.g. 'iMessage;-;+15551234567'")
    s.add_argument("text", help="message text")

    args = p.parse_args(argv)

    if not Path(args.config).exists():
        print(
            f"config not found: {args.config} — "
            "copy config.example.toml to config.toml first."
        )
        return 2

    cfg = Config.load(args.config)
    _setup_logging(cfg, args.verbose)

    if args.cmd == "run":
        return _cmd_run(cfg)
    if args.cmd == "chats":
        return _cmd_chats(cfg)
    if args.cmd == "send":
        return _cmd_send(args.chat_guid, args.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
