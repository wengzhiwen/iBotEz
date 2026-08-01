# iBotEz

`English` | [`中文`](README.zh-CN.md) | [`日本語`](README.ja.md)

**A minimal iMessage ⇄ [Pi](https://pi.dev) bridge for macOS.**

iBotEz watches the local `~/Library/Messages/chat.db` for new iMessages from **whitelisted** contacts, forwards each one to a local [Pi](https://pi.dev) agent (RPC mode), and texts Pi's reply back through Messages.app. iBotEz is *just the bridge* — Pi does all the thinking (models, skills, tools).

## Features

- **Text bridge**: incoming iMessage → Pi → reply, plus **cron-scheduled** Pi tasks that proactively push results to a contact.
- **Whitelist by phone/email**, managed with an interactive `chats` picker.
- **Adaptive polling** of chat.db (2s → 15s backoff) that is **WAL-aware** (never misses new messages).
- **Health watchdog** that self-restarts if the Pi subprocess dies or the worker stalls.
- **Slow-turn handling**: periodic progress reports + bounded retry when Pi stalls.
- **Per-contact Pi sessions**, resumed across restarts.
- **Zero runtime dependencies** — Python 3.11+ standard library only.

## Requirements

- **macOS** (built/tested on 26.x) with **Messages.app** signed in to iMessage
- **Python 3.11+**
- **[Pi](https://pi.dev)** installed and configured with a model provider (`pi config`)
- **Full Disk Access** granted to the Python interpreter that runs iBotEz (required to read chat.db)
- **Automation** permission for controlling Messages.app (prompted on first send)

## Quick start

```bash
git clone https://github.com/wengzhiwen/iBotEz.git
cd iBotEz
python3.14 -m venv venv        # any Python 3.11+ works
cp config.example.toml config.toml
venv/bin/python -m ibotez chats   # list conversations, pick ones to whitelist
venv/bin/python -m ibotez run     # start the bridge
```

(Or `pip install -e .` and use the `ibotez` command.) Then send an iMessage from a whitelisted contact — iBotEz replies via Pi.

## How it works

```
contact ──iMessage──▶ Messages.app ──▶ chat.db
                                        │  (polled, WAL-aware)
iBotEz ──prompt──▶ Pi (RPC) ──reply──▶ iBotEz ──osascript──▶ Messages.app ──▶ contact
```

- chat.db is polled **read-only** every `interval_seconds`, tracking a high-watermark. The first run skips the backlog.
- Each whitelisted contact maps to its **own Pi session**, resumed across restarts via `state.json`.
- Replies are sent with AppleScript through Messages.app; iBotEz never touches iMessage credentials.

## Configuration

Everything lives in `config.toml` (see `config.example.toml`):

| Section | Keys (defaults) |
|---|---|
| `[poll]` | `interval_seconds` (2), `max_interval_seconds` (15), `backoff_factor` (1.5) |
| `[imessage]` | `db_path` (`~/Library/Messages/chat.db`) |
| `[pi]` | `command` (`["pi","--mode","rpc"]`), `progress_interval_seconds` (30), `no_progress_timeout_seconds` (120), `max_retries` (2), `append_instruction` (true) |
| `[bridge]` | `allow` (whitelist of phones/emails), `reply_on_error` |
| `[[schedule]]` | `cron`, `prompt`, `to`, `name` |
| `[health]` | `check_seconds` (5), `stall_seconds` (600), `max_depth` (100) |
| `[log]` | `file`, `level` (`INFO`) |

Whitelist matching: phones compare on the **last 10 digits**, emails are lowercased — so `+1 (555) 123-4567` and `5551234567` are the same contact.

## Scheduled tasks

Run a Pi prompt on a cron schedule and send the result to a contact:

```toml
[[schedule]]
name = "morning-forex"
cron = "0 9 * * *"               # 5-field: min hour dom mon dow (0=Sun); supports *, */N, N, N-M, N,M
prompt = "Summarize today's USD/JPY forex news."
to = "+8613xxxxxxxx"             # a contact with an existing iMessage conversation
```

## ⚠️ Important limitation: sending only works interactively

iBotEz **must run in a foreground / GUI session** (Terminal, tmux). macOS **silently blocks scripted iMessage sending from background `launchd` daemons** (the AppleScript returns success but the message is never delivered), and homebrew's venv interpreter hangs under launchd. Therefore:

- Run interactively: `venv/bin/python -m ibotez run`
- For auto-restart, wrap it: `while true; do venv/bin/python -m ibotez run; sleep 5; done`

This bot is **text-only**: file attachments can't be delivered programmatically either, so Pi is instructed to refuse file-generation / file-send requests.

## Security

Pi is a coding agent with **Bash / Read / Write / Edit** tools. Bridging iMessage to it means a whitelisted contact can — via Pi — run commands on your Mac. Keep the whitelist to numbers you control.

## License

[MIT](LICENSE)
