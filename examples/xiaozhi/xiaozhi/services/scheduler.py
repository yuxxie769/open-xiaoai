import json
import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from config import APP_CONFIG
from xiaozhi.ref import get_speaker, get_xiaoai, get_xiaozhi
from xiaozhi.services.protocols.typing import AbortReason, DeviceState

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

ScheduleType = Literal["interval", "daily"]
ScheduleAction = Literal["play_url", "play_tts", "ask_xiaoai", "chat_xiaozhi"]


@dataclass(frozen=True)
class ScheduleJob:
    name: str
    type: ScheduleType
    action: ScheduleAction
    enabled: bool = True

    # interval
    every_seconds: int | None = None

    # daily
    at: str | None = None  # "HH:MM"

    # payload
    url: str | None = None
    text: str | None = None

    # speaker options
    wake_up: bool = True
    silent_wake: bool = True
    blocking: bool = True

    # xiaozhi options
    abort_before: bool = False


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.strip().split(":", 1)
    hour = int(hour_str)
    minute = int(minute_str)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time out of range")
    return hour, minute


def _next_daily_run(now: datetime, at: str) -> datetime:
    hour, minute = _parse_hhmm(at)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


def _parse_timezone(value: str | None):
    if not value:
        return None

    tz = str(value).strip()
    if not tz or tz.lower() in {"local", "system", "default"}:
        return None

    # Support fixed-offset forms: "+08:00", "UTC+8", "GMT-0530", etc.
    upper = tz.upper()
    for prefix in ("UTC", "GMT"):
        if upper.startswith(prefix):
            tz = tz[len(prefix) :].strip()
            break

    if tz and tz[0] in {"+", "-"}:
        sign = 1 if tz[0] == "+" else -1
        rest = tz[1:]
        if ":" in rest:
            hours_str, minutes_str = rest.split(":", 1)
        elif len(rest) in {1, 2}:
            hours_str, minutes_str = rest, "0"
        elif len(rest) == 4:
            hours_str, minutes_str = rest[:2], rest[2:]
        else:
            hours_str, minutes_str = "", ""
        try:
            hours = int(hours_str)
            minutes = int(minutes_str)
            offset = timedelta(hours=hours, minutes=minutes) * sign
            return timezone(offset)
        except Exception:
            return None

    if ZoneInfo is None:
        return None

    try:
        return ZoneInfo(tz)
    except Exception:
        return None


async def _execute_job(job: ScheduleJob):
    speaker = get_speaker()
    if speaker is None:
        return

    if job.wake_up:
        await speaker.wake_up(True, silent=job.silent_wake)

    if job.action == "play_url":
        if job.url:
            await speaker.play(url=job.url, blocking=job.blocking)
        return

    if job.action == "play_tts":
        if job.text:
            await speaker.play(text=job.text, blocking=job.blocking)
        return

    if job.action == "ask_xiaoai":
        if job.text:
            await speaker.ask_xiaoai(job.text, silent=job.silent_wake)
        return


async def _execute_xiaozhi_job(job: ScheduleJob):
    xiaozhi = get_xiaozhi()
    if xiaozhi is None or getattr(xiaozhi, "protocol", None) is None:
        return

    protocol = xiaozhi.protocol

    try:
        if hasattr(protocol, "open_audio_channel"):
            await protocol.open_audio_channel()
    except Exception:
        return

    try:
        if job.abort_before and getattr(xiaozhi, "device_state", None) == DeviceState.SPEAKING:
            await protocol.send_abort_speaking(AbortReason.ABORT)
    except Exception:
        pass

    if job.action == "chat_xiaozhi" and job.text:
        session_id = getattr(protocol, "session_id", "") or ""
        await protocol.send_text(
            json.dumps(
                {
                    "type": "listen",
                    "state": "detect",
                    "text": job.text,
                    "source": "text",
                    "session_id": session_id,
                },
                ensure_ascii=False,
            )
        )


class Scheduler:
    _stop_event = threading.Event()
    _threads: list[threading.Thread] = []
    _started = False
    _tzinfo = None

    @classmethod
    def start(cls):
        if cls._started:
            return

        schedule_config = APP_CONFIG.get("schedule")
        jobs = cls._load_jobs(schedule_config)
        if not jobs:
            return

        cls._tzinfo = _parse_timezone(
            schedule_config.get("timezone") if isinstance(schedule_config, dict) else None
        )
        cls._stop_event.clear()
        cls._threads = []

        for job in jobs:
            thread = threading.Thread(
                target=cls._job_loop,
                args=(job,),
                daemon=True,
                name=f"schedule:{job.name}",
            )
            thread.start()
            cls._threads.append(thread)

        cls._started = True

    @classmethod
    def stop(cls):
        if not cls._started:
            return

        cls._stop_event.set()
        for thread in cls._threads:
            thread.join(timeout=0.5)
        cls._threads = []
        cls._started = False

    @classmethod
    def _job_loop(cls, job: ScheduleJob):
        while not cls._stop_event.is_set():
            if not job.enabled:
                time.sleep(1)
                continue

            if job.action == "chat_xiaozhi":
                xiaozhi = get_xiaozhi()
                loop = getattr(xiaozhi, "loop", None) if xiaozhi else None
                if loop is None:
                    time.sleep(0.2)
                    continue
                execute = _execute_xiaozhi_job
            else:
                xiaoai = get_xiaoai()
                loop = getattr(xiaoai, "async_loop", None) if xiaoai else None
                if loop is None:
                    time.sleep(0.2)
                    continue
                execute = _execute_job

            try:
                if job.type == "interval":
                    if not job.every_seconds or job.every_seconds <= 0:
                        time.sleep(1)
                        continue
                    if cls._stop_event.wait(timeout=job.every_seconds):
                        return
                elif job.type == "daily":
                    if not job.at:
                        time.sleep(1)
                        continue
                    now = datetime.now(tz=cls._tzinfo)
                    next_run = _next_daily_run(now, job.at)
                    sleep_seconds = max(0.0, (next_run - now).total_seconds())
                    if cls._stop_event.wait(timeout=sleep_seconds):
                        return
                else:
                    time.sleep(1)
                    continue

                asyncio.run_coroutine_threadsafe(execute(job), loop)
            except Exception:
                time.sleep(1)

    @classmethod
    def _load_jobs(cls, schedule_config: Any) -> list[ScheduleJob]:
        if not isinstance(schedule_config, dict):
            return []

        raw_jobs = schedule_config.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            return []

        jobs: list[ScheduleJob] = []
        for idx, raw in enumerate(raw_jobs):
            if not isinstance(raw, dict):
                continue

            try:
                name = str(raw.get("name") or f"job_{idx}")
                jobs.append(
                    ScheduleJob(
                        name=name,
                        type=raw.get("type"),
                        action=raw.get("action"),
                        enabled=bool(raw.get("enabled", True)),
                        every_seconds=raw.get("every_seconds"),
                        at=raw.get("at"),
                        url=raw.get("url"),
                        text=raw.get("text"),
                        wake_up=bool(raw.get("wake_up", True)),
                        silent_wake=bool(raw.get("silent_wake", True)),
                        blocking=bool(raw.get("blocking", True)),
                        abort_before=bool(raw.get("abort_before", False)),
                    )
                )
            except Exception:
                continue

        return jobs
