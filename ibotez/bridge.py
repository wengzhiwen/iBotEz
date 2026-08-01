"""Bridge orchestration.

    poller (adaptive)  ─┐
    scheduler (cron)   ─┼──> asyncio.Queue[Job]  ──>  worker (serial, one Pi turn at a time)
                                                                ^
    a health watchdog supervises: if the Pi subprocess dies, or the worker stalls
    with a non-empty queue, it raises ServiceRestart so the process exits NON-ZERO
    and a supervisor relaunches iBotEz.

While a Pi turn is slow, the worker sends periodic progress iMessages and, if Pi
stalls, aborts and retries a bounded number of times.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from . import imessage
from .config import Config
from .job import Job
from .pi import PiRpcClient, PiRpcError, PiStalled
from .session_map import SessionMap

log = logging.getLogger("ibotez.bridge")

# Appended to Pi's system prompt: this bot can only send plain-text iMessages,
# so Pi should refuse file-generate / file-send requests and explain why.
_PI_INSTRUCTION = (
    "你通过 iMessage 与用户对话，只能发送纯文本消息，无法发送任何文件或附件"
    "（图片、PDF、PPT、文档等均不可）。因此当用户要求你「生成/制作/导出/保存一个文件」"
    "或「把文件发给我」时，请直接说明：本对话无法通过 iMessage 发送文件，所以你不会生成"
    "文件；请改为用文字给出内容、步骤或代码。不要生成 pptx/pdf/图片等文件。"
)


class ServiceRestart(Exception):
    """Raised on an unhealthy state; the process should exit non-zero so the
    supervisor restarts it."""


class Health:
    """Shared counters/timestamps for the watchdog."""

    def __init__(self) -> None:
        self.last_progress = time.monotonic()
        self.enqueued = 0
        self.processed = 0

    def mark_progress(self) -> None:
        self.last_progress = time.monotonic()
        self.processed += 1


# -- iMessage send helper (non-blocking) -------------------------------------

async def _send_imessage(chat_guid: str, text: str, health: Health) -> None:
    """Send an iMessage off the event loop; refreshes the watchdog heartbeat."""
    if not chat_guid:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, imessage.send, chat_guid, text)
    health.last_progress = time.monotonic()


# -- Pi turn with progress reports + bounded retry ---------------------------

async def _abort_quietly(pi: PiRpcClient) -> None:
    with contextlib.suppress(Exception):
        await pi.abort()


async def _run_pi_with_progress(
    pi: PiRpcClient, job: Job, cfg: Config, health: Health
) -> tuple[str | None, str | None]:
    """Run pi.prompt for job.prompt. Sends progress iMessages while it works and
    aborts+retries on stall. Returns (reply_text, error)."""
    progress = cfg.progress_interval
    chunk = progress if progress > 0 else 3600.0

    for attempt in range(cfg.max_retries + 1):
        task = asyncio.create_task(pi.prompt(job.prompt, stall_timeout=cfg.no_progress_timeout))
        start = time.monotonic()
        try:
            while True:
                try:
                    text = await asyncio.wait_for(asyncio.shield(task), chunk)
                    return text, None
                except asyncio.TimeoutError:
                    if progress > 0:
                        elapsed = int(time.monotonic() - start)
                        await _send_imessage(
                            job.reply_to,
                            f"⏳ Pi 仍在工作…（{pi.current_status}，已 {elapsed}s）",
                            health,
                        )
        except PiStalled:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            await _abort_quietly(pi)
            if attempt < cfg.max_retries:
                if progress > 0:
                    await _send_imessage(
                        job.reply_to,
                        f"⚠️ Pi 似乎卡住，重试中（{attempt + 1}/{cfg.max_retries}）",
                        health,
                    )
                continue
            return None, f"pi stalled after {cfg.max_retries} retries"
        except PiRpcError as e:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            if attempt < cfg.max_retries:
                continue
            return None, str(e)
    return None, "max retries exceeded"


# -- job handling ------------------------------------------------------------

async def _handle_job(
    pi: PiRpcClient, state: SessionMap, cfg: Config, job: Job, health: Health
) -> None:
    log.info("job %s -> pi (session=%s)", job.label, job.session_key)

    session_file = state.sessions.get(job.session_key)
    try:
        if session_file:
            await pi.switch_session(session_file)
        else:
            await pi.new_session(name=job.session_name or job.session_key)

        text, error = await _run_pi_with_progress(pi, job, cfg, health)

        if session_file is None:
            try:
                state.sessions[job.session_key] = await pi.current_session_file()
                state.save()
            except Exception:
                log.exception("could not capture Pi session file for %s", job.session_key)

        if text:
            await _send_imessage(job.reply_to, text, health)
            log.info("replied (%s): %r", job.label, text[:80])
        elif error:
            log.warning("job %s ended with error: %s", job.label, error)
            if cfg.reply_on_error:
                await _send_imessage(job.reply_to, cfg.reply_on_error, health)
        else:
            log.info("no text reply for %s (tool-only or empty)", job.label)

    except Exception as e:  # the bridge must stay up across job failures
        log.exception("failed to handle job %s: %s", job.label, e)
        if cfg.reply_on_error:
            with contextlib.suppress(Exception):
                await _send_imessage(job.reply_to, cfg.reply_on_error, health)


# -- producers / consumers ---------------------------------------------------

async def poller(
    cfg: Config,
    state: SessionMap,
    allow: set[str],
    queue: asyncio.Queue,
    health: Health,
) -> None:
    interval = cfg.interval
    while True:
        new = 0
        try:
            con = imessage.connect(cfg.db_path)
            try:
                msgs = imessage.fetch_since(con, state.watermark)
            finally:
                con.close()
        except Exception:
            log.exception("poll failed")
            msgs = []

        for m in msgs:
            # Advance the watermark for every new message so non-whitelisted
            # ones don't pile up to be re-evaluated forever.
            if m.date > state.watermark:
                state.watermark = m.date
                state.save()
            key = imessage.norm_contact(m.sender)
            if key not in allow:
                continue
            await queue.put(Job(
                session_key=key,
                prompt=m.text or "",
                reply_to=m.chat_guid or "",
                label=f"from {m.sender}",
                session_name=m.sender or None,
            ))
            health.enqueued += 1
            new += 1

        # Adaptive backoff: reset to the floor on activity, else grow to the cap.
        interval = cfg.interval if new else min(cfg.max_interval, interval * cfg.backoff_factor)
        log.debug("poll +%d new, next=%.1fs queue=%d", new, interval, queue.qsize())
        await asyncio.sleep(interval)


async def worker(
    cfg: Config, state: SessionMap, pi: PiRpcClient, queue: asyncio.Queue, health: Health
) -> None:
    while True:
        job: Job = await queue.get()
        health.mark_progress()  # counts as progress even before Pi answers
        try:
            await _handle_job(pi, state, cfg, job, health)
        finally:
            queue.task_done()


async def monitor(
    cfg: Config, pi: PiRpcClient, queue: asyncio.Queue, health: Health
) -> None:
    ticks = 0
    heartbeat = max(1, int(round(60.0 / cfg.health_check_seconds)))
    while True:
        await asyncio.sleep(cfg.health_check_seconds)
        ticks += 1
        depth = queue.qsize()

        # Rule 1: the Pi subprocess died -> we can't process anything -> restart.
        if not pi.is_alive():
            rc = pi.proc.returncode if pi.proc else None
            raise ServiceRestart(f"Pi subprocess exited (returncode={rc})")

        # Rule 2: messages are queued but the worker hasn't made progress ->
        # it's stuck (e.g. Pi hung) -> restart.
        idle = time.monotonic() - health.last_progress
        if depth > 0 and idle > cfg.stall_seconds:
            raise ServiceRestart(f"worker stalled: queue={depth}, no progress for {idle:.0f}s")

        if depth >= cfg.max_depth:
            log.warning("queue depth %d >= threshold %d", depth, cfg.max_depth)
        elif ticks % heartbeat == 0:
            log.info(
                "health ok: pi=alive queue=%d enqueued=%d processed=%d",
                depth, health.enqueued, health.processed,
            )


async def run(cfg: Config) -> None:
    allow = cfg.allow_set
    if not allow:
        log.warning(
            "whitelist is empty — nothing will be bridged. "
            "Run `python -m ibotez chats` to add contacts."
        )

    state = SessionMap.load(cfg.state_path)

    pi_command = list(cfg.pi_command)
    if cfg.pi_append_instruction:
        pi_command += ["--append-system-prompt", _PI_INSTRUCTION]
        log.info("Pi launch includes iMessage capability instruction")
    pi = PiRpcClient(pi_command, cfg.pi_cwd)
    try:
        await pi.start()

        if state.watermark == 0:
            con = imessage.connect(cfg.db_path)
            try:
                state.watermark = imessage.max_date(con)
            finally:
                con.close()
            state.save()
            log.info("first run — watermark set to %d (backlog skipped)", state.watermark)

        log.info(
            "bridge running (poll %.1f->%.1fs adaptive, %d whitelisted, %d scheduled)",
            cfg.interval, cfg.max_interval, len(allow), len(cfg.schedules),
        )

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()

        def _on_signal() -> None:
            log.info("shutdown signal received; stopping cleanly (exit 0)")
            shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (NotImplementedError, RuntimeError):
                pass  # signal handling unavailable; rely on default behavior

        queue: asyncio.Queue[Job] = asyncio.Queue()
        health = Health()

        # Local import to avoid a bridge<->scheduler circular import at load time.
        from .scheduler import scheduler as scheduler_task

        restart_reason: ServiceRestart | None = None
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(poller(cfg, state, allow, queue, health), name="poller"),
                    tg.create_task(worker(cfg, state, pi, queue, health), name="worker"),
                    tg.create_task(scheduler_task(cfg.schedules, cfg.db_path, queue, health), name="scheduler"),
                    tg.create_task(monitor(cfg, pi, queue, health), name="monitor"),
                ]

                async def stop_watcher() -> None:
                    await shutdown.wait()
                    for t in tasks:
                        t.cancel()

                tg.create_task(stop_watcher(), name="stop_watcher")
        except* ServiceRestart as eg:
            first = eg.exceptions[0] if eg.exceptions else None
            restart_reason = ServiceRestart(str(first) if first else "unknown")
    finally:
        await pi.aclose()

    if restart_reason is not None:
        log.error(
            "exiting for restart (%s) — the supervisor should relaunch iBotEz",
            restart_reason,
        )
        raise SystemExit(1)
