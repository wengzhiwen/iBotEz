"""iMessage I/O: read-only chat.db polling + AppleScript sending."""
from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

# message.date is nanoseconds since 2001-01-01 UTC.
_MAC_EPOCH_OFFSET_S = 978307200  # seconds between 1970-01-01 and 2001-01-01
_MAC_EPOCH_OFFSET_MS = _MAC_EPOCH_OFFSET_S * 1000


def norm_contact(s: str | None) -> str:
    """Normalize a phone/email for whitelist matching.

    Emails -> lowercased. Phone-like strings -> last 10 digits.
    """
    s = (s or "").strip()
    if not s:
        return ""
    if "@" in s:
        return s.lower()
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if len(digits) >= 10 else s.lower()


def date_to_unix_ms(date_val: int) -> float:
    """Convert a chat.db mac-epoch nanosecond timestamp to unix milliseconds."""
    return date_val / 1e6 + _MAC_EPOCH_OFFSET_MS


@dataclass
class InMessage:
    rowid: int
    guid: str
    text: str
    date: int
    sender: str | None       # handle.id of the sender (phone/email)
    chat_guid: str | None    # chat GUID, used as the AppleScript `chat id` target


@dataclass
class ChatInfo:
    rowid: int
    guid: str
    identifier: str | None
    display_name: str | None
    handles: list[str]
    last_text: str | None
    last_date: int


def connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(
            f"chat.db not found at {p} — is Full Disk Access granted to the "
            "Python interpreter / terminal running iBotEz?"
        )
    # Open a normal connection so SQLite applies the WAL journal. Messages.app
    # runs chat.db in WAL mode, so `immutable=1` would ignore chat.db-wal and
    # only see a stale checkpointed snapshot (i.e. miss new messages).
    # query_only guards against accidental writes; busy_timeout rides out the
    # brief exclusive lock during a WAL checkpoint.
    con = sqlite3.connect(str(p), timeout=30.0)
    con.execute("PRAGMA query_only = ON")
    return con


_FETCH_SQL = """
SELECT m.ROWID, m.guid, m.text, m.date, h.id AS sender, c.guid AS chat_guid
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c               ON c.ROWID        = cmj.chat_id
LEFT JOIN handle h         ON h.ROWID        = m.handle_id
WHERE m.date > :watermark
  AND m.is_from_me = 0
  AND m.text IS NOT NULL
  AND m.item_type = 0
ORDER BY m.date ASC
"""


def fetch_since(con: sqlite3.Connection, watermark: int) -> list[InMessage]:
    rows = con.execute(_FETCH_SQL, {"watermark": watermark}).fetchall()
    return [InMessage(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


def max_date(con: sqlite3.Connection) -> int:
    """Newest message date — used to skip the backlog on first run."""
    return int(con.execute("SELECT COALESCE(MAX(date), 0) FROM message").fetchone()[0])


_HANDLE_SQL = """
SELECT chj.chat_id, h.id
FROM chat_handle_join chj
JOIN handle h ON h.ROWID = chj.handle_id
"""

_INBOUND_HANDLE_SQL = """
SELECT cmj.chat_id, h.id
FROM chat_message_join cmj
JOIN message m ON m.ROWID = cmj.message_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.is_from_me = 0 AND h.id IS NOT NULL
"""

_LIST_SQL = """
SELECT c.ROWID, c.guid, c.chat_identifier, c.display_name, m.text, m.date
FROM chat c
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN message m             ON m.ROWID        = cmj.message_id
ORDER BY m.date DESC
"""


def list_chats(con: sqlite3.Connection) -> list[ChatInfo]:
    """All conversations, newest first, with their contact handle(s)."""
    chj: dict[int, set[str]] = {}
    for cid, hid in con.execute(_HANDLE_SQL):
        chj.setdefault(cid, set()).add(hid)
    inbound: dict[int, set[str]] = {}
    for cid, hid in con.execute(_INBOUND_HANDLE_SQL):
        inbound.setdefault(cid, set()).add(hid)

    latest: dict[int, tuple] = {}
    for cid, guid, ident, disp, text, date in con.execute(_LIST_SQL):
        if cid not in latest:  # ordered DESC, so first seen = newest
            latest[cid] = (guid, ident, disp, text, date)

    out = []
    for cid, (guid, ident, disp, text, date) in latest.items():
        hs = chj.get(cid) or inbound.get(cid) or set()
        out.append(ChatInfo(cid, guid, ident, disp, sorted(hs), text, date))
    out.sort(key=lambda c: c.last_date, reverse=True)
    return out


_SEND_SCRIPT = (
    'on run argv\n'
    '  tell application "Messages" to send (item 2 of argv) '
    'to chat id (item 1 of argv)\n'
    'end run'
)


def chat_guid_for(db_path: str, handle: str) -> str | None:
    """Resolve a phone/email (or contact-name-ish string) to its chat_guid in
    chat.db. Used by scheduled tasks to know where to send results. Returns
    None if no existing conversation matches."""
    target = norm_contact(handle)
    if not target:
        return None
    try:
        con = connect(db_path)
    except Exception:
        return None
    try:
        # 1) exact handle match -> its chat (chat_handle_join, then message handles)
        for (hid,) in con.execute("SELECT id FROM handle"):
            if norm_contact(hid) == target:
                row = con.execute(
                    "SELECT c.guid FROM chat c "
                    "JOIN chat_handle_join chj ON chj.chat_id = c.ROWID "
                    "JOIN handle h ON h.ROWID = chj.handle_id WHERE h.id = ? LIMIT 1",
                    (hid,),
                ).fetchone()
                if row:
                    return row[0]
                row = con.execute(
                    "SELECT c.guid FROM chat c "
                    "JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID "
                    "JOIN message m ON m.ROWID = cmj.message_id "
                    "JOIN handle h ON h.ROWID = m.handle_id WHERE h.id = ? LIMIT 1",
                    (hid,),
                ).fetchone()
                if row:
                    return row[0]
        # 2) fallback: contact name in chat_identifier / display_name
        like = f"%{handle.strip()}%"
        row = con.execute(
            "SELECT guid FROM chat WHERE chat_identifier LIKE ? OR display_name LIKE ? LIMIT 1",
            (like, like),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def send(chat_guid: str, text: str) -> None:
    """Send `text` to an iMessage chat via Messages.app (AppleScript).

    Args are passed as argv (not interpolated) to avoid quoting issues. A
    timeout prevents the worker from blocking forever if macOS is waiting on an
    Automation permission decision (common under launchd).
    """
    try:
        subprocess.run(
            ["osascript", "-e", _SEND_SCRIPT, "--", chat_guid, text],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "osascript send timed out (>20s) — likely an Automation permission "
            "prompt blocking under launchd"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise RuntimeError(f"osascript send failed (exit {e.returncode}): {stderr}") from e

