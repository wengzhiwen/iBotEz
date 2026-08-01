# iBotEz

[`English`](README.md) | `中文` | [`日本語`](README.ja.md)

**一个极简的 iMessage ⇄ [Pi](https://pi.dev) 桥（macOS）。**

iBotEz 监听本机的 `~/Library/Messages/chat.db`，一旦**白名单联系人**发来新的 iMessage，就把它转发给本机的 [Pi](https://pi.dev) agent（RPC 模式），再把 Pi 的回复通过 Messages.app 发回去。iBotEz **只是个桥**——思考（模型、技能、工具）全由 Pi 完成。

## 功能

- **文本桥**：收到 iMessage → Pi → 回复；并支持 **cron 定时任务**，主动把 Pi 的结果推给联系人。
- **按手机号 / 邮箱设白名单**，可用交互式 `chats` 命令挑选。
- **自适应轮询** chat.db（2s → 15s 退避），**感知 WAL**（绝不漏新消息）。
- **健康看门狗**：Pi 进程死亡或 worker 卡住时自动重启。
- **慢任务处理**：定期回报进度 + 卡住时有限次自动重试。
- **每个联系人独立 Pi 会话**，重启后可恢复。
- **零运行时依赖**——仅用 Python 3.11+ 标准库。

## 环境要求

- **macOS**（在 26.x 上构建/测试），**Messages.app** 已登录 iMessage
- **Python 3.11+**
- 已安装 **[Pi](https://pi.dev)** 并配好模型供应商（`pi config`）
- 给运行 iBotEz 的 Python 解释器授予**完全磁盘访问**（读 chat.db 必须）
- 控制 Messages.app 的**自动化**权限（首次发送时弹窗）

## 快速开始

```bash
git clone https://github.com/wengzhiwen/iBotEz.git
cd iBotEz
python3.14 -m venv venv        # 任何 Python 3.11+ 都行
cp config.example.toml config.toml
venv/bin/python -m ibotez chats   # 列出会话，选择要加入白名单的
venv/bin/python -m ibotez run     # 启动桥
```

（或 `pip install -e .` 后用 `ibotez` 命令。）然后从白名单联系人发条 iMessage——iBotEz 会用 Pi 回复。

## 工作原理

```
联系人 ──iMessage──▶ Messages.app ──▶ chat.db
                                       │（轮询，感知 WAL）
iBotEz ──prompt──▶ Pi (RPC) ──回复──▶ iBotEz ──osascript──▶ Messages.app ──▶ 联系人
```

- 每 `interval_seconds` **只读**轮询 chat.db，用一个水位（watermark）增量取新消息。首次启动跳过历史积压。
- 每个白名单联系人对应**独立的 Pi 会话**，经 `state.json` 在重启后恢复。
- 回复用 AppleScript 经 Messages.app 发出，iBotEz 不碰任何 iMessage 凭证。

## 配置

全部在 `config.toml`（模板见 `config.example.toml`）：

| 段 | 键（默认值） |
|---|---|
| `[poll]` | `interval_seconds`(2)、`max_interval_seconds`(15)、`backoff_factor`(1.5) |
| `[imessage]` | `db_path`(`~/Library/Messages/chat.db`) |
| `[pi]` | `command`(`["pi","--mode","rpc"]`)、`progress_interval_seconds`(30)、`no_progress_timeout_seconds`(120)、`max_retries`(2)、`append_instruction`(true) |
| `[bridge]` | `allow`(白名单手机号/邮箱)、`reply_on_error` |
| `[[schedule]]` | `cron`、`prompt`、`to`、`name` |
| `[health]` | `check_seconds`(5)、`stall_seconds`(600)、`max_depth`(100) |
| `[log]` | `file`、`level`(`INFO`) |

白名单匹配：手机号取**后 10 位**比较，邮箱转小写——`+1 (555) 123-4567` 与 `5551234567` 视为同一联系人。

## 定时任务

按 cron 计划跑一个 Pi 提示词，并把结果发给联系人：

```toml
[[schedule]]
name = "morning-forex"
cron = "0 9 * * *"               # 5 段：分 时 日 月 周（0=周日）；支持 *、*/N、N、N-M、N,M
prompt = "请总结今日美元/日元汇率新闻。"
to = "+8613xxxxxxxx"             # 已有 iMessage 会话的联系人
```

## ⚠️ 重要限制：发送只能在交互式会话里进行

iBotEz **必须在前台 / GUI 会话**（终端、tmux）里运行。macOS 会**静默拦截后台 launchd 守护进程脚本化发送 iMessage**（AppleScript 返回成功但消息永远不送达），且 homebrew 的 venv 解释器在 launchd 下会启动死锁。因此：

- 交互式运行：`venv/bin/python -m ibotez run`
- 要自动重启，套个循环：`while true; do venv/bin/python -m ibotez run; sleep 5; done`

本 bot **仅支持文本**：文件附件也无法程序化投递，所以已告知 Pi 直接拒绝「生成/发送文件」类请求。

## 安全

Pi 是带 **Bash/Read/Write/Edit** 工具的编码 agent。把 iMessage 桥给它意味着白名单联系人能（经由 Pi）在你的 Mac 上执行命令。白名单请只放你自己掌控的号码。

## 许可证

[MIT](LICENSE)
