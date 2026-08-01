"""Pi (pi.dev) RPC client — talks to a long-lived `pi --mode rpc` subprocess
over stdin/stdout JSON Lines.

Pi is spawned with subprocess.Popen rather than asyncio.create_subprocess_exec:
the asyncio child watcher can deadlock when iBotEz runs as a launchd daemon
(the daemon's initial signal mask differs from an interactive shell's). Popen
reaps the child via waitpid directly, with no such dependency. Blocking I/O is
bridged to awaitables through a thread executor, so the public API stays fully
async.

`prompt()` exposes a live ``current_status`` (for progress reports) and raises
``PiStalled`` when Pi produces no event for ``stall_timeout`` seconds, so the
caller can report progress / abort-and-retry a stuck turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time

log = logging.getLogger("ibotez.pi")


class PiRpcError(RuntimeError):
    pass


class PiStalled(PiRpcError):
    """Pi produced no event for stall_timeout seconds (likely stuck)."""
    pass


def _assistant_text(message: dict) -> str:
    """Concatenate text blocks of an assistant message."""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts).strip()


class PiRpcClient:
    def __init__(self, command: list[str], cwd: str | None = None):
        self.command = command
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self._counter = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self.current_status: str = "启动中"

    def _exec(self, fn, *args):
        return self._loop.run_in_executor(None, fn, *args)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.proc = await self._exec(self._popen)
        self.current_status = "已就绪"
        log.info("Pi started (pid=%s): %s", self.proc.pid, " ".join(self.command))
        self._loop.create_task(self._drain_stderr())

    def _popen(self) -> subprocess.Popen:
        return subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self._exec(self.proc.stderr.readline)
            if not line:
                break
            log.debug("pi stderr: %s", line.decode(errors="replace").rstrip())

    async def aclose(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self._exec(self._wait_or_kill), timeout=10)
            except asyncio.TimeoutError:
                pass  # _wait_or_kill already killed

    def _wait_or_kill(self) -> None:
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def is_alive(self) -> bool:
        """True if the Pi subprocess is still running."""
        return self.proc is not None and self.proc.poll() is None

    # -- low-level JSONL I/O ------------------------------------------------

    def _next_id(self) -> str:
        self._counter += 1
        return f"r{self._counter}"

    def _write_sync(self, data: bytes) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    async def _send(self, obj: dict) -> None:
        await self._exec(self._write_sync, (json.dumps(obj) + "\n").encode())

    async def _readobj(self, timeout: float = 180.0) -> dict | None:
        """Read one JSON object. Returns None on timeout (stall); raises
        PiRpcError if the Pi stream closed (subprocess exited)."""
        assert self.proc and self.proc.stdout
        try:
            line = await asyncio.wait_for(self._exec(self.proc.stdout.readline), timeout)
        except asyncio.TimeoutError:
            return None
        if not line:
            raise PiRpcError("Pi stream closed (subprocess exited)")
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            log.warning("non-JSON line from pi: %r", line[:200])
            return {}

    async def _cmd(self, obj: dict, timeout: float = 20.0) -> dict:
        """Send a control command; return its matching `response` object."""
        rid = obj.get("id") or self._next_id()
        obj["id"] = rid
        await self._send(obj)
        while True:
            o = await self._readobj(timeout)
            if o is None:
                raise PiRpcError(f"no response for {obj.get('type')} (id={rid})")
            if o.get("type") == "response" and o.get("id") == rid:
                if not o.get("success", True):
                    raise PiRpcError(f"pi rejected {obj.get('type')}: {o.get('error')}")
                return o
            # ignore streamed events while waiting for the ack

    # -- session lifecycle --------------------------------------------------

    async def new_session(self, name: str | None = None) -> None:
        await self._cmd({"type": "new_session"})
        if name:
            try:
                await self._cmd({"type": "set_session_name", "name": name})
            except PiRpcError:
                pass  # naming is cosmetic; don't fail the turn

    async def switch_session(self, session_path: str) -> None:
        await self._cmd({"type": "switch_session", "sessionPath": session_path})

    async def current_session_file(self) -> str:
        o = await self._cmd({"type": "get_state"})
        data = o.get("data") or {}
        sf = data.get("sessionFile")
        if not sf:
            raise PiRpcError("get_state returned no sessionFile")
        return sf

    async def abort(self, drain_seconds: float = 15.0) -> None:
        """Abort the current turn and drain events until Pi is idle again, so a
        fresh prompt can be issued (used on stall/retry)."""
        self.current_status = "正在中止"
        try:
            await self._send({"type": "abort"})
        except Exception:
            return
        deadline = time.monotonic() + drain_seconds
        while time.monotonic() < deadline:
            o = await self._readobj(max(1.0, deadline - time.monotonic()))
            if o is None or o.get("type") == "agent_settled":
                break
        self.current_status = "已中止"

    # -- the main act -------------------------------------------------------

    async def prompt(self, message: str, stall_timeout: float = 120.0) -> str:
        """Send a user prompt; return the assistant's final text reply.

        ``current_status`` is updated as events stream (for progress reports).
        Raises PiStalled if no event arrives within ``stall_timeout`` seconds.
        """
        rid = self._next_id()
        self.current_status = "已发送，等待响应"
        await self._send({"id": rid, "type": "prompt", "message": message})
        last_text = ""
        error_seen: str | None = None
        while True:
            o = await self._readobj(stall_timeout)
            if o is None:
                raise PiStalled(f"no Pi event for {stall_timeout:.0f}s")
            t = o.get("type")

            # live status for progress reports
            if t == "tool_execution_start":
                self.current_status = f"调用工具 {o.get('toolName')}"
            elif t == "message_update":
                ame = o.get("assistantMessageEvent") or {}
                at = ame.get("type") or ""
                if at == "error":
                    error_seen = ame.get("error") or error_seen
                elif at.startswith("thinking"):
                    self.current_status = "思考中"
                elif at.startswith("text"):
                    self.current_status = "生成回复中"
                elif at.startswith("toolcall"):
                    self.current_status = "准备调用工具"

            if t in ("message_end", "turn_end"):
                msg = o.get("message") or {}
                if msg.get("role") == "assistant":
                    txt = _assistant_text(msg)
                    if txt:
                        last_text = txt

            elif t == "agent_end":
                for m in reversed(o.get("messages") or []):
                    if m.get("role") == "assistant":
                        txt = _assistant_text(m)
                        if txt:
                            last_text = txt
                            break
                if not o.get("willRetry"):
                    break  # done — final attempt, no more retries

            elif t == "agent_settled":
                break

            elif t == "auto_retry_end" and o.get("success") is False:
                error_seen = o.get("finalError") or error_seen

        self.current_status = "完成"
        if not last_text and error_seen:
            raise PiRpcError(f"pi produced no reply: {error_seen}")
        return last_text
