# iBotEz

A minimal **iMessage ⇄ [Pi](https://pi.dev)** message bridge for macOS.

iBotEz watches `~/Library/Messages/chat.db` for new iMessages from **whitelisted**
contacts, forwards each one to a local Pi agent (RPC mode), and texts Pi's reply
back through Messages.app. iBotEz is *just the bridge* — Pi does all the thinking
(models, skills, tools).

## Requirements

- macOS (built/tested on 26.x) with **Messages.app** signed in to iMessage
- **Python 3.11+** (a 3.14 venv is included in `venv/`)
- **[Pi](https://pi.dev)** installed and configured with a model provider (`pi config`)
- **Full Disk Access** granted to the Python interpreter / terminal that runs iBotEz
  *(otherwise chat.db reads silently return empty)*
- **Automation** permission for controlling Messages.app *(prompted on first send)*

## Setup

```bash
cp config.example.toml config.toml
venv/bin/python -m ibotez chats      # list conversations, pick ones to whitelist
venv/bin/python -m ibotez run        # start the bridge
```

(Or `source venv/bin/activate` first, then drop the `venv/bin/` prefix.)

## Commands

| Command | Description |
|---|---|
| `python -m ibotez chats` | List conversations; interactively add senders to the whitelist |
| `python -m ibotez run` | Start polling and bridging |
| `python -m ibotez send "<chat_guid>" "text"` | Send a test message to a chat |

## How it works

- Reads chat.db **read-only** every `interval_seconds`, tracking a high-watermark.
- **First run skips the backlog** (watermark = newest message at startup) so it
  never replies to old history.
- Each whitelisted contact maps to its **own Pi session**, resumed across restarts
  via `state.json` (`switch_session`).
- Replies are sent with AppleScript through Messages.app; iBotEz never touches
  iMessage credentials.

Whitelist matching: phone numbers compare on the **last 10 digits**, emails are
lowercased — so `+1 (555) 123-4567` and `5551234567` are the same contact.

## ⚠️ Security

Pi is a coding agent with **Bash / Read / Write / Edit** tools. Bridging iMessage
to it means a whitelisted contact can — via Pi — run commands on your Mac. Keep
the whitelist to numbers you control, and be aware Pi may call tools in response
to a message. See [`docs/design.md`](docs/design.md) for the full design.

## 定时任务（主动推送）

在 `config.toml` 加 `[[schedule]]` 表，iBotEz 会按 cron 时间自动跑 Pi 提示词，并把结果发给指定联系人：

```toml
[[schedule]]
name = "morning-forex"
cron = "0 9 * * *"            # 5 段：分 时 日 月 周（0=周日）；支持 *  */N  N  N-M  N,M
prompt = "请简洁总结今日美元兑日元汇率的重要新闻"
to = "+8613xxxxxxxx"          # 你的号码/邮箱（需已有 iMessage 会话，运行时在 chat.db 解析 chat_guid）
```

每个任务有独立的 Pi 会话（跨次保留上下文），并与收信回复共用同一套「进度回报 + 重试」。

## 慢任务进度回报 & 卡住重试

Pi 回复慢时，每隔 `progress_interval_seconds`（默认 30s）给你发一条进度（含 Pi 当前状态：思考中 / 调用工具… / 生成回复中）。Pi 长时间无动静（`no_progress_timeout_seconds`，默认 120s）视为卡住 → 自动中止并重试，最多 `max_retries` 次（默认 2），仍失败则发兜底回复。配置在 `[pi]` 下：

```toml
progress_interval_seconds = 30      # 0 = 关闭进度回报
no_progress_timeout_seconds = 120
max_retries = 2
```

## 仅文本（无法发送文件）

这个 bot 只能通过 iMessage 发送**纯文本**——macOS 不允许程序化发送文件附件（AppleScript/Shortcuts 都会被拦截或失败），所以文件发送功能没有实现。iBotEz 启动时会向 Pi 注入一条能力说明：当用户要求**生成/导出/保存文件**或**把文件发过来**时，Pi 会直接告知"无法通过 iMessage 发送文件"，并改为用文字给出内容、步骤或代码。可在 `[pi]` 下用 `append_instruction = false` 关掉这条注入。

## 运行方式（重要）

iBotEz **必须在前台 / GUI 会话里运行**（终端、tmux，或未来的 .app 登录项）：

- macOS 会**静默拦截后台 launchd 守护进程**对 iMessage 的脚本化"发送"（osascript 返回成功但消息不发出去）；
- 且 homebrew 的 **venv python 在 launchd 下启动会死锁**。

所以：

```bash
# 推荐：终端里前台跑（收发全通）
venv/bin/python -m ibotez run

# 崩溃自启：Pi 挂时内置看门狗让进程非零退出，循环拉起
while true; do venv/bin/python -m ibotez run; echo "restarting in 5s…"; sleep 5; done
```

- 需要**完全磁盘访问**（授予运行 iBotEz 的 python 解释器）才能读 `~/Library/Messages/chat.db`。
- `deploy/com.ibotez.bridge.plist` 仅作参考保留：launchd 下「收信 + Pi」可用，但**发不出消息**。
- 想要「开机自启 + 能发送」的守护进程，需把 iBotEz 打成 `.app` 加到登录项（在 GUI 会话里运行）——待办。
