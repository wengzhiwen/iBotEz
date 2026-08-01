# iBotEz — iMessage ⇄ Pi 桥接器 设计文档

> 版本: v0.1 (设计稿)　·　日期: 2026-07-29　·　状态: 待评审

## 1. 目标与非目标

### 1.1 一句话定位
iBotEz 是一个跑在本机 Mac 上的**极简消息桥**：把指定 iMessage 会话里收到的文本转发给本机的 [Pi](https://pi.dev)（编码 agent harness）处理，再把 Pi 的回复发回 iMessage。iBotEz **不含任何模型交互、不含任何技能逻辑**——那些都是 Pi 的职责。

### 1.2 目标
- **收**：从 `~/Library/Messages/chat.db` 只读轮询新消息（白名单会话）。
- **发**：通过 AppleScript（osascript）经 Messages.app 把回复发回原会话。
- **大脑**：spawn 一个常驻的 `pi --mode rpc` 子进程，经 stdin/stdout 的 JSONL 与之通信；每个 iMessage 会话映射到一个可恢复的 Pi session。
- **简单**：纯 Python 标准库、单进程、零运行时第三方依赖、总代码量目标 < 400 行。
- **可运行于 Python 3.14**（已就绪 venv）。

### 1.3 非目标（显式排除）
- 不做 LLM 调用 / prompt 管理 / 技能 —— 全部交给 Pi。
- 不做多平台（Telegram/WhatsApp 等）——只 iMessage。
- 不做附件收发（v1 只文本；附件作为未来增强）。
- 不做 Web UI / 管理面板 —— 一个 CLI 进程 + 一个 TOML 配置。
- 不重新发明 OpenClaw 的 channel-server —— iBotEz 自己内联了"驱动 Pi"的逻辑（主动转发器形态）。

### 1.4 参考与选型依据
- **OpenClaw / Pi 架构**：Pi 是"组件集合"，OpenClaw 把 Pi 接到通信渠道。iBotEz 等于自己实现一个"iMessage 渠道 + 驱动"的最小版本。([lucumr.pocoo.org](https://lucumr.pocoo.org/2026/1/31/pi/))
- **参考实现** `greghughespdx/imessage-bridge`（OpenClaw 的 iMessage 桥组件）：~200 行、纯 stdlib、chat.db 只读 + AppleScript 发送。我们的 iMessage I/O 与其同源。
- **Pi RPC 协议**：`pi --mode rpc` 走 stdin/stdout JSONL。([pi.dev/docs/latest/rpc](https://pi.dev/docs/latest/rpc))
- **Pi 会话**：JSONL 树结构，存于 `~/.pi/agent/sessions/`，可按文件路径恢复。([pi.dev/docs/latest/sessions](https://pi.dev/docs/latest/sessions))

---

## 2. 总体架构

```
                        ┌──────────────── iBotEz (Python 3.14, asyncio) ────────────────┐
                        │                                                                │
   每 N 秒轮询           │   ┌──────────┐    ┌──────────────┐    ┌────────────────────┐   │
   ┌────────────┐  RO    │   │ Poller   │──▶ │ Worker(队列) │──▶ │  PiRpcClient        │   │
   │ chat.db    │◀───────┼───│ sqlite3  │    │ 串行执行     │    │  spawn `pi --mode   │   │
   │ 只读       │        │   │ +白名单  │    │ 同时仅1个    │    │  rpc` 长驻子进程     │   │
   └────────────┘        │   └────┬─────┘    └──────────────┘    │  stdin/stdout JSONL │   │
                        │        │                          ┌───┤  会话恢复/累积回复   │   │
                        │        ▼                          │   └─────────┬──────────┘   │
                        │   ┌──────────────┐  持久化 JSON    │             │              │
                        │   │ SessionMap   │◀───────────────┘             │              │
                        │   │ chat_guid →  │     watermark 也持久化         │              │
                        │   │ pi session   │                              │              │
                        │   └──────────────┘                              │              │
                        └──────────────────────────────────────────────────┼──────────────┘
                                                                           │
                                          ┌────────────────────────────────┤
                                          ▼  osascript                     ▼ spawn
                                   ┌──────────────┐               ┌──────────────────┐
                                   │ Messages.app │               │  pi --mode rpc   │ (Node.js)
                                   │  (仅用于发)   │               │  Bash/Read/Write │
                                   └──────┬───────┘               │  /Edit 工具       │
                                          │                       └────────┬─────────┘
                                          ▼                                ▼
                                   iMessage 云 ⇄ 联系人           模型供应商(Anthropic…)
```

### 2.1 三大支柱
| 支柱 | 职责 | 实现 |
|---|---|---|
| **iMessage I/O** | 读 chat.db / 发 Messages.app | `sqlite3`（stdlib）+ `subprocess` 调 `osascript` |
| **Pi RPC 客户端** | 与常驻 Pi 子进程 JSONL 通信 | `asyncio.subprocess` + JSON Lines |
| **Bridge 编排** | 轮询 → 白名单 → 入队 → 驱动 Pi → 回写 | `asyncio` 任务 + 持久化的 `SessionMap` |

---

## 3. 组件设计

### 3.1 `imessage.py` — iMessage 读写

**数据库连接（普通连接，必须能读到 WAL）：**
```python
# Messages.app 把 chat.db 跑在 WAL 模式——新消息先写进 chat.db-wal。
# 若用 immutable=1，SQLite 会忽略 -wal，只看到上次 checkpoint 的旧快照，
# 于是永远收不到新消息。用普通连接 + query_only + busy_timeout：
DB = "~/Library/Messages/chat.db"
con = sqlite3.connect(str(Path(DB).expanduser()), timeout=30.0)
con.execute("PRAGMA query_only = ON")   # 永不写
```

**轮询查询（带 chat_guid + 发件人，按水位增量）：**
```sql
SELECT m.ROWID, m.guid, m.text, m.date, h.id AS sender, c.guid AS chat_guid
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c               ON c.ROWID        = cmj.chat_id
LEFT JOIN handle h         ON h.ROWID        = m.handle_id
WHERE m.date       > :watermark      -- mac NSDate 纳秒，见下
  AND m.is_from_me = 0               -- 只收别人发的
  AND m.text IS NOT NULL             -- 过滤纯附件/撤回/typing
  AND m.item_type  = 0               -- 排除 typing indicator 等系统项
ORDER BY m.date ASC;
```
> Python 侧再用白名单集合过滤 `chat_guid`（也可直接 `AND c.guid IN (...)`）。

**`date` 列换算**：`message.date` 是「自 2001-01-01 UTC 起的纳秒数」。
```
unix_seconds = date / 1e9 + 978307200   # 978307200 = 2001↔1970 偏移
```
水位 `watermark` 直接存 mac NSDate 纳秒整数即可（无需换算，只要比较时同量纲）。

**发送（AppleScript，参数化避免转义地狱）：**
```python
subprocess.run(
    ["osascript",
     "-e", "on run argv",
     "-e", 'tell application "Messages" to send (item 2 of argv) to chat id (item 1 of argv)',
     "-e", "end run",
     "--", chat_guid, text],
    check=True,
)
```
- 用 `chat id`（来自 chat.db 的 `c.guid`，形如 `iMessage;-;+15551234567`），最稳。
- 文本作为 argv 传入，绕开 shell 引号转义。
- 群聊 GUID 形如 `iMessage;+;chat123456789`，同样适用（回复发到群里）。

### 3.2 `pi.py` — Pi RPC 客户端

**启动常驻子进程：**
```python
proc = await asyncio.create_subprocess_exec(
    *cfg.pi.command,           # ["pi", "--mode", "rpc"]，可加 --provider/--model
    cwd=cfg.pi.cwd,            # 建议指向一个独立工作目录（安全）
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
)
```
- provider/model **默认不传**，让 Pi 自己的配置决定；可在 `config.toml` 覆盖。
- 建议传 `--session-dir` 指向项目内目录，便于隔离/备份。

**请求（stdin，一行一个 JSON）：**
```jsonl
{"id":"req-1","type":"prompt","message":"<iMessage 原文>"}
```

**读响应（stdout，逐行解析事件，直到 `agent_settled`）：**
- 累积 `message_update` 事件里 `assistantMessageEvent.type == "text_delta"` 的 `delta` → 拼成回复正文。
- `{"type":"agent_settled"}` = 本轮彻底结束（无重试/无 follow-up），可回写 iMessage。
- 期间若有 `tool_execution_*` 事件（Pi 调 Bash 等），**对 iBotEz 透明**——iBotEz 只关心最终文本回复。
- 错误：`{"type":"auto_retry_end","success":false,...}` / `assistantMessageEvent.type == "error"` → 把错误摘要作为回复或丢弃并记日志。

**会话映射（每个 chat_guid 一个 Pi session）：**
| 场景 | 动作 |
|---|---|
| 该 chat **首次**消息 | 直接 `prompt`（Pi 默认新建 session）→ 再发 `{"type":"get_state"}` 取 `sessionFile` → 存入 SessionMap → 可选 `{"type":"set_session_name","name":"<chat_guid>"}` 便于排查 |
| 该 chat **已有** session（含重启后从持久化恢复） | 先 `{"type":"switch_session","sessionPath":"<file>"}` 再 `prompt` |
| 切换到**不同** chat | `switch_session` 后再 `prompt` |

**并发模型**：Worker **串行**，同一时刻仅 1 个 prompt 在 Pi 中执行。理由：Pi RPC 是单 session 状态机；串行最简单且正确。多 chat 消息在队列里排队，可接受（聊天 bot 延迟容忍度高）。

### 3.3 `bridge.py` — 编排核心
```
loop:
    msgs = imessage.fetch_since(watermark)          # 增量拉取
    for m in msgs:
        if m.chat_guid not in whitelist: continue
        watermark = max(watermark, m.date)          # 推进水位（先推进，避免崩溃重发）
        await queue.put(m)                          # 入队
    drain: worker 从队列取 → PiRpcClient.handle(msg, session_map) → imessage.send(reply)
    persist(watermark, session_map)                 # 落盘
    sleep(interval)
```
- **首启动水位**：取 `max(date)` 作为初始水位，**跳过历史积压**，只处理启动后真正的新消息（否则会把整段聊天史全回一遍）。
- 水位 + SessionMap 定期落盘到 `state.json`，崩溃/重启可恢复。

### 3.4 `session_map.py` — 持久化映射
```json
{
  "watermark": 715234567890123456,
  "chats": {
    "iMessage;-;+15551234567": "/Users/.../.pi/agent/sessions/abc.jsonl"
  }
}
```

### 3.5 `config.py` — 配置加载（stdlib `tomllib`）

`config.toml`：
```toml
[poll]
interval_seconds = 2

[imessage]
db_path = "~/Library/Messages/chat.db"

[pi]
command    = ["pi", "--mode", "rpc"]
cwd        = "."                       # Pi 的工作目录（建议独立沙箱目录）
session_dir = "./.pi-sessions"         # 传 --session-dir，可选

[bridge]
# 白名单：只桥接这些 chat_guid（来自 chat.db 的 c.guid）
allow = [
  "iMessage;-;+15551234567",
  # "iMessage;+;chat123456789",   # 群聊
]
reply_on_error = "（处理失败，请稍后再试）"   # Pi 出错时的兜底回复；留空则不回
```

---

## 4. 数据流（时序）

```
联系人 ──iMessage──▶ Messages.app ──写入──▶ chat.db
                                                 │
iBotEz Poller ──增量SELECT──▶ [msg{chat_guid,text,sender}]
        │ 白名单过滤 + 推进水位 + 入队
        ▼
Worker ──switch_session(按需)──▶ Pi RPC(stdin)
        ──prompt{text}──────────▶ Pi
        ◀── message_update(text_delta) ×N ──── Pi（可能含 tool_execution：跑 Bash 等）
        ◀── agent_settled ──────────────────── Pi
        │ 累积 deltas = reply
        ▼
imessage.send(chat_guid, reply) ──osascript──▶ Messages.app ──iMessage──▶ 联系人
```

---

## 5. ⚠️ 安全（务必先读）

> **核心风险：Pi 是带 Bash/Read/Write/Edit 的编码 agent。把 iMessage 文本桥给它，等于让白名单内的发件人能（经由 Pi）在这台 Mac 上执行任意 shell 命令、读写文件。**

即便白名单只有一个联系人，也要假设该账号可能被盗/误操作。缓解措施（建议至少做到前两条）：

1. **白名单极小化**：只放完全信任的、你本人的号码；不要放大群。
2. **沙箱化 Pi**：用独立低权用户、容器、或受限目录运行；`cwd` 指向无关痛痒的空目录；`--session-dir` 隔离。
3. **限制 Pi 能力**：配置 Pi 的技能/profile，必要时禁用 Bash 或加确认（Pi 支持 self-extension，可写一个「危险命令需确认」的扩展——但 v1 先靠配置）。
4. **不要暴露端口**：iBotEz 不监听任何网络端口（主动转发器，纯本地）。
5. **审计**：所有 iMessage→Pi 的 prompt 与 Pi 的 tool 调用落本地日志，便于事后审查。
6. **API key 隔离**：Pi 用的模型 API key 不要是主力账号的、设好消费上限。

---

## 6. 前置条件 / 环境准备

| 项 | 说明 |
|---|---|
| ✅ Python 3.14 | 已装（`/opt/homebrew/bin/python3.14`），venv 已建 |
| ⬜ Pi | 尚未安装。需 `pi`（Node.js）+ 配好一个模型供应商（API key）。iBotEz 在 Pi 可用前无法端到端跑通 |
| ⬜ **完全磁盘访问** | 系统设置 → 隐私与安全性 → 完全磁盘访问 → 勾选**启动 iBotEz 的那个解释器/终端**（venv 的 python 是软链到 homebrew python，需给 `/opt/homebrew/bin/python3.14` 或 Terminal/iTerm 授权）。否则 chat.db 查询静默返回空 |
| ⬜ **自动化权限** | 首次 osascript 控制 Messages.app 时系统弹窗「Python 想要控制 Messages」，必须允许。可在 隐私与安全性 → 自动化 里事后改 |
| ⬜ Messages.app | 须已登录 iCloud/iMessage 并保持运行（发送依赖它） |
| ⬜ chat_guid 确认 | 用一次性脚本查 chat.db 拿到目标会话的 `c.guid`，填进 `config.toml` 白名单 |

> macOS 26.5.1 上 chat.db 受 SIP/FDA 保护，上面两个权限是硬性前提，无法绕过。

---

## 7. 项目结构（拟）

```
iBotEz/
├── venv/                     # ✅ 已创建（Python 3.14.3）
├── docs/
│   └── design.md             # ← 本文档
├── ibotez/
│   ├── __init__.py
│   ├── __main__.py           # 入口：python -m ibotez
│   ├── config.py             # tomllib 加载 config.toml
│   ├── imessage.py           # chat.db 读 + osascript 发
│   ├── pi.py                 # Pi RPC 客户端（asyncio + JSONL）
│   ├── bridge.py             # 轮询 + Worker + 编排
│   └── session_map.py        # state.json 读写
├── config.example.toml
├── pyproject.toml            # 元数据，无运行时第三方依赖
├── README.md
└── .gitignore                # 至少忽略 venv/、state.json、.pi-sessions/
```

预计总代码量 **300–400 行**，依赖：**仅 Python 标准库**。

---

## 8. 实现阶段（文档通过后）

1. **脚手架**：`pyproject.toml`、包结构、`config.example.toml`、`.gitignore`、`README.md`。
2. **iMessage 读**：`imessage.fetch_since()` + 一个 `python -m ibotez.tools.dump-chats` 小工具列出 chat_guid（辅助填白名单）。
3. **iMessage 发**：`imessage.send()` + osascript，手动单测给某会话发一条。
4. **Pi RPC 客户端**：spawn、prompt、累积 text_delta、agent_settled、switch_session/get_state。（此步需 Pi 已装）
5. **SessionMap + 水位持久化**：`state.json`。
6. **Bridge 编排**：轮询 + 串行 Worker，端到端跑通。
7. **健壮性**：错误兜底回复、日志、优雅退出、首启动跳积压。

---

## 9. 待你确认的开放问题

1. **Pi 状态**：你打算什么时候装 Pi？v1 实现里 Pi 客户端可先用「mock Pi」(echo) 开发，等 Pi 就绪再接真进程——是否要这样分两步？
2. **白名单粒度**：白名单填 `chat_guid` 够用，还是想要「按手机号/联系人名」更友好（需要额外解析 handle.id）？
3. **Pi 工作目录 / 安全策略**：接受第 5 节的沙箱建议吗？要不要 v1 就给 Pi 禁用 Bash（在 Pi 配置侧）？
4. **群聊**：v1 要不要支持群（多 sender 时把 `sender` 注入 prompt 前缀，如 `[+1555…] 文本`）？还是先只做 1v1？
5. **回复长度**：Pi 回复可能很长，要不要设上限/分段发送？

---

## 10. 未来增强（非 v1）
- 附件：读 chat.db 的 attachment → base64 → Pi `prompt.images`（多模态）。
- 从被动 HTTP 桥模式（对齐 OpenClaw `/messages` `/send`）作为可选后端，便于别的 channel 接入。
- 用 FSEvents / 文件监听替代轮询，降低延迟。
- 多 Pi 并发（每 chat 一个 Pi 进程）以并行回复。
- Web/命令行管理面板。

---

## 11. 决策确认与实测验证（v0.2，2026-07-29）

### 11.1 已确认的设计决策
- **iMessage 接入**：直连 chat.db（只读）+ AppleScript 发送。
- **桥接形态**：主动转发器（iBotEz 是 Pi 的客户端）。
- **转发范围**：白名单按**手机号 / 邮箱**（手机号匹配后 10 位，邮箱小写）；提供 `chats` 交互工具列出会话并加入白名单。
- **沙箱**：不开。Pi 以项目目录为 cwd 运行。
- **群聊**：v1 不支持（只桥 1:1）。
- **回复长度**：不设限。
- **Pi 状态**：已安装（**0.82.1**，默认模型 **gpt-5.6-luna / openai**）。

### 11.2 已实测验证（本机）
- ✅ 字节编译 + 全模块导入通过。
- ✅ `chats` 能读真实 chat.db 并列出会话（发件人号、最后消息、时间）。
- ✅ 白名单写入 `config.toml` + 规范化重载正确（`+8618…` → 后 10 位匹配）。
- ✅ PiRpcClient 会话生命周期（`new_session` / `get_state` / `switch_session` / `aclose`）离线跑通。
- ✅ RPC wire format 经探针实证（见 11.3）。
- ⚠️ 未在本环境实测：① 真实 iMessage **发送**（会真发消息给联系人 + 需「自动化」权限）；② prompt→LLM 回复（沙箱无外网）。两者代码路径已按实测协议编写，待你在联网、授权环境跑一次即可。

### 11.3 实测的 Pi RPC wire format（0.82.1）
- 控制命令响应信封：`{"id","type":"response","command","success","data":{...}}`
  - `get_state` → `data.sessionFile` / `data.sessionId` / `data.model`
  - `get_available_models` → `data.models[]`
- prompt 事件流：`response`(ack) → `agent_start` → `turn_start` → `message_start/end`(user) → `message_update`(`assistantMessageEvent.type=text_delta`) → `message_end`(assistant, `content:[{type:"text",text}]`) → `turn_end` → `agent_end{messages:[...],willRetry}` → `agent_settled`
- 会话按 cwd 分目录：`~/.pi/agent/sessions/--<cwd 路径>--/<时间戳>_<uuid>.jsonl`
- 回复文本取自**最后一个 assistant message** 的 text blocks（比累积 delta 稳）。

### 11.4 运行方式
```bash
cp config.example.toml config.toml
venv/bin/python -m ibotez chats     # 选会话加入白名单
venv/bin/python -m ibotez run       # 启动桥
```
