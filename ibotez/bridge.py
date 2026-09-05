"""Bridge orchestration.

    poller (adaptive)  ─┐
    scheduler (cron)   ─┼──> asyncio.Queue[Job]  ──>  worker (serial, one Pi turn at a time)
                                                                ^
    a health watchdog supervises: if the Pi subprocess dies, or the worker stalls
    with a non-empty queue, it raises ServiceRestart so the process exits NON-ZERO
    and a supervisor relaunches iBotEz. A scheduled built-in task ([[schedule]]
    builtin = "pi-update") runs `pi update`, iMessages a briefing, and restarts
    the same way when an upgrade lands.

While a Pi turn is slow, the worker sends periodic progress iMessages and, if Pi
stalls, aborts and retries a bounded number of times.

Users can address numbered Pi sessions with a leading "！N" (full-width exclamation
+ 1-3 digits): each (contact, N) pair keeps its own Pi session, and Pi's reply is
prefixed with 【N】. Messages without the prefix use the contact's default session.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from dataclasses import replace
from datetime import datetime

from . import imessage
from .config import Config
from .job import Job
from .pi import PiRpcClient, PiRpcError, PiStalled
from .scheduler import SchedState
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


class WatermarkTracker:
    """Advances the persisted watermark only as jobs complete (at-least-once).

    The poller used to advance the watermark the moment a message was *read*;
    with an in-memory queue, a restart between "saved" and "job done" lost the
    message forever. Now a message's date is marked pending when its job is
    enqueued and committed when the job finishes; the watermark folds up to the
    newest committed date that is still older than the oldest pending one, so
    unfinished work is re-fetched after a restart (worst case: one duplicate
    reply if the process died between sending it and committing).
    """

    def __init__(self, state: SessionMap) -> None:
        self._state = state
        self._pending: set[int] = set()    # dates of enqueued-but-unfinished jobs
        self._committed: set[int] = set()  # finished dates not yet folded in

    def pending(self, date: int) -> None:
        """Mark `date` as having an unfinished job gating the watermark."""
        if date:
            self._pending.add(date)

    def commit(self, date: int) -> None:
        """Mark `date` finished (job done or no job needed); fold the watermark."""
        if not date:
            return
        self._pending.discard(date)
        self._committed.add(date)
        if self._pending:
            bound = min(self._pending)
            candidates = [d for d in self._committed if d < bound]
            watermark = max(candidates) if candidates else None
        else:
            watermark = max(self._committed)
        if watermark is not None and watermark > self._state.watermark:
            self._state.watermark = watermark
            try:
                self._state.save()
            except Exception:
                # A failed save only means the watermark lags (possible
                # duplicate replies later) — never worth killing the bridge.
                log.exception("could not persist watermark")
        self._committed = {d for d in self._committed if d > self._state.watermark}


# -- iMessage send helper (non-blocking) -------------------------------------

async def _send_imessage(chat_guid: str, text: str, health: Health) -> None:
    """Send an iMessage off the event loop; refreshes the watchdog heartbeat."""
    if not chat_guid:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, imessage.send, chat_guid, text)
    except Exception:
        log.exception("send to %s failed: %r", chat_guid, text[:80])
        raise
    log.info("sent to %s: %r", chat_guid, text[:80])
    health.last_progress = time.monotonic()


# -- Pi turn with progress reports + bounded retry ---------------------------

async def _abort_quietly(pi: PiRpcClient) -> None:
    with contextlib.suppress(Exception):
        await pi.abort()


def _swallow(task: asyncio.Task) -> None:
    """Retrieve (and discard) a finished task's exception so asyncio doesn't
    log "Task exception was never retrieved" for shielded prompt() tasks."""
    if not task.cancelled():
        task.exception()


async def _run_pi_with_progress(
    pi: PiRpcClient, job: Job, cfg: Config, health: Health
) -> tuple[str | None, str | None]:
    """Run pi.prompt for job.prompt. Sends progress iMessages while it works and
    aborts+retries on stall. Returns (reply_text, error)."""
    progress = cfg.progress_interval
    chunk = progress if progress > 0 else 3600.0

    for attempt in range(cfg.max_retries + 1):
        task = asyncio.create_task(pi.prompt(job.prompt, stall_timeout=cfg.no_progress_timeout))
        task.add_done_callback(_swallow)
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
                log.warning(
                    "attempt %d/%d for %s stalled (%s) — retrying",
                    attempt + 1, cfg.max_retries + 1, job.label, pi.current_status,
                )
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
                log.warning(
                    "attempt %d/%d for %s failed: %s — retrying",
                    attempt + 1, cfg.max_retries + 1, job.label, e,
                )
                continue
            log.error("attempt %d/%d for %s failed: %s — giving up",
                      attempt + 1, cfg.max_retries + 1, job.label, e)
            return None, str(e)
    return None, "max retries exceeded"


# -- job handling ------------------------------------------------------------

# Scheduled-task retry: hold strong refs to sleeping re-enqueue tasks so they
# aren't garbage-collected mid-sleep.
_retry_tasks: set[asyncio.Task] = set()


def _schedule_retry(
    delay_minutes: float, job: Job, queue: asyncio.Queue, health: Health
) -> None:
    """Re-enqueue `job` on the worker queue after `delay_minutes`."""

    async def _later() -> None:
        await asyncio.sleep(delay_minutes * 60.0)
        await queue.put(job)
        health.enqueued += 1
        log.info("scheduled-task retry re-enqueued: %s", job.label)

    t = asyncio.create_task(_later())

    def _done(task: asyncio.Task) -> None:
        _retry_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve, so asyncio doesn't log it as unhandled

    _retry_tasks.add(t)
    t.add_done_callback(_done)


async def _report_scheduled_failure(
    sched_state: SchedState, cfg: Config, job: Job,
    queue: asyncio.Queue, health: Health, error: str,
) -> None:
    """A scheduled task failed: notify with the task name and error, retry with
    backoff (re-enqueue after cfg.schedule_retry_minutes), and when the retries
    run out send a final notice saying when the task last succeeded."""
    name = job.schedule_name
    delays = cfg.schedule_retry_minutes
    if job.retry_attempt < len(delays):
        delay = delays[job.retry_attempt]
        # Schedule the retry FIRST: if the notice below fails to send, the
        # retry must still happen (a transient iMessage hiccup must not lose
        # the task for the day).
        retry = replace(
            job,
            retry_attempt=job.retry_attempt + 1,
            label=f"{job.label}#r{job.retry_attempt + 1}",
        )
        _schedule_retry(delay, retry, queue, health)
        with contextlib.suppress(Exception):
            await _send_imessage(
                job.reply_to,
                f"⚠️ 定时任务「{name}」失败：{error}。{delay:g} 分钟后自动重试"
                f"（{job.retry_attempt + 1}/{len(delays)}）",
                health,
            )
    else:
        last = sched_state.last_success.get(name)
        last_str = "无记录"
        if last:
            with contextlib.suppress(ValueError):
                last_str = datetime.fromisoformat(last).strftime("%m-%d %H:%M")
        log.error(
            "scheduled task %r gave up after %d retries: %s (last success %s)",
            name, len(delays), error, last_str,
        )
        # Record the give-up so the startup catch-up pass doesn't re-run a
        # task that already exhausted its retries today.
        with contextlib.suppress(Exception):
            sched_state.last_giveup[name] = (
                datetime.now().astimezone().isoformat(timespec="seconds")
            )
            sched_state.save()
        with contextlib.suppress(Exception):
            await _send_imessage(
                job.reply_to,
                f"❌ 定时任务「{name}」未能完成（已重试 {len(delays)} 次，最后错误：{error}）。"
                f"上次成功：{last_str}",
                health,
            )

async def _run_pi_update(
    cfg: Config, job: Job, queue: asyncio.Queue, health: Health
) -> None:
    """Built-in task: run `pi update` once, iMessage a briefing to job.reply_to,
    and when an upgrade landed raise ServiceRestart (once the queue has drained)
    so the supervisor relaunches iBotEz on the new version."""
    cmd = [cfg.pi_command[0], "update"]
    log.info("job %s: running %s ...", job.label, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
    except Exception as e:
        log.exception("pi update failed to run")
        await _send_imessage(job.reply_to, f"⚠️ Pi 自动升级执行失败（{e}），下个周期再试。", health)
        return
    for line in text.strip().splitlines():
        log.info("pi update: %s", line)
    if proc.returncode != 0:
        log.warning("pi update exited with code %d", proc.returncode)
        await _send_imessage(
            job.reply_to,
            f"⚠️ Pi 自动升级失败（pi update 退出码 {proc.returncode}），下个周期再试。",
            health,
        )
        return

    m = re.search(r"Updated pi from (\S+) to (\S+)", text)
    if not m:
        await _send_imessage(job.reply_to, "✅ Pi 自动升级检查完成：已是最新版本。", health)
        return

    old, new = m.group(1), m.group(2)
    await _send_imessage(
        job.reply_to,
        f"⬆️ Pi 已自动升级：{old} → {new}。iBotEz 将在空闲时重启以启用新版本。",
        health,
    )

    # Upgraded: wait for queued jobs to drain so we don't kill a running turn
    # (the worker is serial and busy with *this* job), then restart.
    log.info("pi upgraded (%s -> %s) — restarting once the queue is drained", old, new)
    while not queue.empty():
        await asyncio.sleep(5)
    raise ServiceRestart(f"pi updated {old} -> {new} — restarting on the new version")


# Appended to a scheduled task's prompt when Pi returned tool calls but no
# text: web-search-heavy report skills occasionally end their turn without
# emitting the report itself.
_NO_TEXT_NUDGE = (
    "\n\n注意：上一轮没有产出任何文字。请直接输出完整的报告文字内容，不要只调用工具。"
)


def _session_size(path: str) -> int:
    """Size of a Pi session file (0 if unreadable)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def _compact_session(pi: PiRpcClient, job: Job, cfg: Config, health: Health) -> None:
    """Run Pi's /compact on the current session to bound its growth. Reply text
    is discarded; a failed compaction never blocks the real prompt."""
    health.last_progress = time.monotonic()  # a long compaction must not look like a stall
    try:
        await pi.prompt("/compact", stall_timeout=cfg.no_progress_timeout)
        log.info("auto-compacted oversized session %s", job.session_key)
    except PiStalled:
        # Pi is still mid-compact-turn: abort it, or the real prompt below
        # would consume the compaction's event stream as its own reply.
        await _abort_quietly(pi)
        log.warning("auto-compact for %s stalled — aborted (continuing)", job.session_key)
    except Exception as e:
        log.warning("auto-compact for %s failed (continuing): %s", job.session_key, e)
    finally:
        health.last_progress = time.monotonic()


async def _handle_job(
    pi: PiRpcClient, state: SessionMap, cfg: Config, job: Job,
    queue: asyncio.Queue, health: Health, sched_state: SchedState,
) -> None:
    if job.builtin:
        if job.builtin == "pi-update":
            await _run_pi_update(cfg, job, queue, health)
        else:
            log.error("job %s: unknown builtin %r — ignored", job.label, job.builtin)
        return

    log.info("job %s -> pi (session=%s)", job.label, job.session_key)

    session_file = state.sessions.get(job.session_key)
    try:
        if session_file:
            await pi.switch_session(session_file)
        else:
            await pi.new_session(name=job.session_name or job.session_key)

        # Scheduled sessions grow on every run (report + tool results append
        # forever); compact oversized ones before this run's prompt.
        if (
            job.is_scheduled
            and session_file
            and cfg.auto_compact_threshold_bytes > 0
            and _session_size(session_file) > cfg.auto_compact_threshold_bytes
        ):
            await _compact_session(pi, job, cfg, health)

        text, error = None, None
        prompt = job.prompt
        # Scheduled tasks get one immediate nudge if Pi ends its turn with tool
        # calls but no text; interactive messages keep the single attempt (the
        # "换个问法" notice below fits a human in the loop, not a cron job).
        attempts = 2 if job.is_scheduled else 1
        for attempt_no in range(attempts):
            text, error = await _run_pi_with_progress(
                pi, replace(job, prompt=prompt), cfg, health
            )
            if text or error:
                break
            if attempt_no + 1 < attempts:
                log.info("no text reply for %s (tool-only or empty) — nudging once", job.label)
                prompt = job.prompt + _NO_TEXT_NUDGE
        if not text and not error and job.is_scheduled:
            error = "Pi 两次均未产出文字"

        if session_file is None:
            try:
                state.sessions[job.session_key] = await pi.current_session_file()
                state.save()
            except Exception:
                log.exception("could not capture Pi session file for %s", job.session_key)

        if text:
            await _send_imessage(job.reply_to, job.reply_prefix + text, health)
            log.info("replied (%s): %r", job.label, text[:80])
            if job.is_scheduled:
                try:
                    sched_state.last_success[job.schedule_name] = (
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    )
                    sched_state.save()
                except Exception:
                    log.exception(
                        "could not record last_success for %s", job.schedule_name
                    )
        elif error:
            log.warning("job %s ended with error: %s", job.label, error)
            if job.is_scheduled:
                await _report_scheduled_failure(
                    sched_state, cfg, job, queue, health, error
                )
            elif cfg.reply_on_error:
                await _send_imessage(job.reply_to, cfg.reply_on_error, health)
        else:
            log.info("no text reply for %s (tool-only or empty)", job.label)
            await _send_imessage(
                job.reply_to,
                f"{job.reply_prefix}（Pi 本轮没有给出文字回复，可以换个问法再试）",
                health,
            )

    except Exception as e:  # the bridge must stay up across job failures
        log.exception("failed to handle job %s: %s", job.label, e)
        if cfg.reply_on_error:
            with contextlib.suppress(Exception):
                await _send_imessage(job.reply_to, cfg.reply_on_error, health)


# -- producers / consumers ---------------------------------------------------

# Session-slot command: a leading full-width "！" + 1-3 digits selects that
# sender's numbered Pi session (e.g. "！12 请继续运算" -> slot 12 of the sender).
# Each (contact, slot) pair maps to its own Pi session, so different iMessage
# accounts never share slots. Messages without the prefix use the contact's
# default session as before.
_SESSION_CMD_RE = re.compile(r"^！(\d{1,3})(?!\d)\s*(.*)$", re.S)


async def poller(
    cfg: Config,
    state: SessionMap,
    allow: set[str],
    queue: asyncio.Queue,
    health: Health,
    tracker: WatermarkTracker,
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
            key = imessage.norm_contact(m.sender)
            if key not in allow:
                # No job will ever finish for this message — commit it now so
                # non-whitelisted messages don't pile up to be re-evaluated.
                tracker.commit(m.date)
                continue

            session_key = key
            prompt = m.text or ""
            session_name = m.sender or None
            reply_prefix = ""
            label = f"from {m.sender}"

            cmd = _SESSION_CMD_RE.match(prompt)
            if cmd:
                slot = int(cmd.group(1))  # int() normalizes "！07" -> slot 7
                prompt = cmd.group(2).strip()
                reply_prefix = f"【{slot}】"
                if not prompt:
                    await _send_imessage(
                        m.chat_guid or "",
                        f"{reply_prefix}请在 ！{slot} 后跟上要发给 Pi 的内容",
                        health,
                    )
                    tracker.commit(m.date)
                    continue
                session_key = f"{key}!{slot}"
                session_name = f"{m.sender} !{slot}"
                label = f"from {m.sender} !{slot}"

            tracker.pending(m.date)
            await queue.put(Job(
                session_key=session_key,
                prompt=prompt,
                reply_to=m.chat_guid or "",
                label=label,
                session_name=session_name,
                reply_prefix=reply_prefix,
                commit_date=m.date,
            ))
            health.enqueued += 1
            new += 1

        # Adaptive backoff: reset to the floor on activity, else grow to the cap.
        interval = cfg.interval if new else min(cfg.max_interval, interval * cfg.backoff_factor)
        log.debug("poll +%d new, next=%.1fs queue=%d", new, interval, queue.qsize())
        await asyncio.sleep(interval)


async def worker(
    cfg: Config, state: SessionMap, pi: PiRpcClient, queue: asyncio.Queue,
    health: Health, sched_state: SchedState, tracker: WatermarkTracker,
) -> None:
    while True:
        job: Job = await queue.get()
        health.mark_progress()  # counts as progress even before Pi answers
        try:
            await _handle_job(pi, state, cfg, job, queue, health, sched_state)
        except asyncio.CancelledError:
            # Shutting down / restarting mid-job: do NOT commit — the source
            # message must be re-fetched and answered after the restart.
            raise
        else:
            # Job finished (success, or a failure already reported to the
            # user): let the watermark advance past its source message.
            # commit(0) is a no-op for scheduled/retry jobs.
            tracker.commit(job.commit_date)
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
    sched_state = SchedState.load(cfg.state_path.parent / "sched_state.json")

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
        tracker = WatermarkTracker(state)

        # Local import to avoid a bridge<->scheduler circular import at load time.
        from .scheduler import scheduler as scheduler_task

        restart_reason: ServiceRestart | None = None
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(poller(cfg, state, allow, queue, health, tracker), name="poller"),
                    tg.create_task(worker(cfg, state, pi, queue, health, sched_state, tracker), name="worker"),
                    tg.create_task(
                        scheduler_task(cfg.schedules, cfg.db_path, queue, health,
                                       sched_state, cfg.catchup_hours),
                        name="scheduler"),
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
