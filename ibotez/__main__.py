"""iBotEz CLI: `run` the bridge, `chats` to manage the whitelist, `send` to test."""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import imessage
from .bridge import run as run_bridge
from .config import Config, write_allow

# Dump all-thread tracebacks to stderr on SIGUSR1 — handy for a stuck daemon.
faulthandler.enable()
faulthandler.register(signal.SIGUSR1, all_threads=True)


def _setup_logging(verbose: bool) -> None:
    root = Path(__file__).resolve().parent.parent
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(root / "logs" / "ibotez.log"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _fmt_date(date_val: int) -> str:
    if not date_val:
        return ""
    ms = imessage.date_to_unix_ms(date_val)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M")


def _cmd_run(args) -> int:
    cfg = Config.load(args.config)
    try:
        asyncio.run(run_bridge(cfg))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def _cmd_chats(args) -> int:
    cfg = Config.load(args.config)
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
        write_allow(args.config, allow)
        print(f"\nAdded {len(added)}: {added}")
        print(f"whitelist now has {len(allow)} entry/entries (saved to {args.config}).")
    else:
        print("Nothing added.")
    return 0


def _cmd_send(args) -> int:
    imessage.send(args.chat_guid, args.text)
    print("sent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ibotez", description="iMessage <-> Pi bridge")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="start the bridge")
    sub.add_parser("chats", help="list conversations and manage the whitelist")
    s = sub.add_parser("send", help="send a test message to a chat_guid")
    s.add_argument("chat_guid")
    s.add_argument("text")

    args = p.parse_args(argv)
    _setup_logging(args.verbose)

    if not Path(args.config).exists():
        print(
            f"config not found: {args.config} — "
            "copy config.example.toml to config.toml first."
        )
        return 2

    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "chats":
        return _cmd_chats(args)
    if args.cmd == "send":
        return _cmd_send(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
