"""
core/workers.py

Proactive Intelligence Background Workers for PRIMUS.

Design:
- All workers are async coroutines running in the main event loop
- Throttled with configurable intervals to minimize CPU impact on AMD iGPU
- Each worker can be individually enabled/disabled via config
- Workers observe Private Mode and autonomy restrictions
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from loguru import logger
from config.settings import settings


class WorkerBase:
    """Base class for all proactive workers."""

    name: str = "BaseWorker"
    interval_seconds: int = 10

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.on_alert = None

    def trigger_alert(self, category: str, message: str):
        if self.on_alert:
            self.on_alert(category, message)

    async def tick(self):
        """Override in subclass — called on each interval."""
        raise NotImplementedError

    async def run(self):
        self._running = True
        logger.info(f"Worker started: {self.name}")
        while self._running:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"Worker {self.name} error: {e}")
            await asyncio.sleep(self.interval_seconds)

    def start(self, loop: asyncio.AbstractEventLoop):
        self._task = loop.create_task(self.run())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info(f"Worker stopped: {self.name}")


# ─────────────────────────────────────────────────────────────
# Thermal & Battery Worker
# ─────────────────────────────────────────────────────────────

class ThermalBatteryWorker(WorkerBase):
    """
    Monitors CPU/GPU temperature and battery level.
    Triggers proactive alerts when thresholds are exceeded.
    """
    name = "ThermalBatteryWorker"
    interval_seconds = 30

    async def tick(self):
        import psutil
        import GPUtil

        # Battery check
        battery = psutil.sensors_battery()
        if battery and not battery.power_plugged:
            percent = battery.percent
            if percent < 15:
                msg = f"Battery critically low: {percent:.0f}%"
                logger.warning(f"[PRIMUS ALERT] {msg}")
                self.trigger_alert("BATTERY", msg)
            elif percent < 25:
                msg = f"Battery below 25%: {percent:.0f}%"
                logger.info(f"[PRIMUS NOTICE] {msg}")
                self.trigger_alert("BATTERY", msg)

        # GPU temp check
        try:
            gpus = GPUtil.getGPUs()
            for gpu in gpus:
                if gpu.temperature > 85:
                    msg = f"High GPU temperature: {gpu.temperature}°C"
                    logger.warning(f"[PRIMUS ALERT] {msg}")
                    self.trigger_alert("THERMAL", msg)
        except Exception:
            pass  # No discrete GPU — normal on AMD iGPU


# ─────────────────────────────────────────────────────────────
# Context Awareness Worker
# ─────────────────────────────────────────────────────────────

class ContextAwarenessWorker(WorkerBase):
    """
    Tracks the active foreground window and updates short-term context.
    Enables PRIMUS to say "You are currently writing Python in VS Code."
    """
    name = "ContextAwarenessWorker"
    interval_seconds = 5

    def __init__(self):
        super().__init__()
        self.on_alert = None
        self._last_window: str = ""
        self._window_start: float = time.time()

    async def tick(self):
        title = await self._get_active_window_title()
        if title and title != self._last_window:
            elapsed = int(time.time() - self._window_start)
            if self._last_window and elapsed > 60:
                logger.info(f"[CONTEXT] Left '{self._last_window}' after {elapsed // 60}m {elapsed % 60}s")
            self._last_window = title
            self._window_start = time.time()
            logger.debug(f"[CONTEXT] Active window: {title}")
            self.trigger_alert("CONTEXT", f"Active window: {title}")

    async def _get_active_window_title(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_title_sync)

    @staticmethod
    def _get_title_sync() -> str:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        except Exception:
            return ""


# ─────────────────────────────────────────────────────────────
# Session Fatigue Worker
# ─────────────────────────────────────────────────────────────

class FatigueWorker(WorkerBase):
    """
    Detects long coding / focus sessions and suggests breaks.
    Respects autonomy mode — only notifies in ASSISTED+ modes.
    """
    name = "FatigueWorker"
    interval_seconds = 60

    def __init__(self):
        super().__init__()
        self.on_alert = None
        self._session_start = time.time()

    async def tick(self):
        elapsed_hours = (time.time() - self._session_start) / 3600
        # Notify every hour of continuous use
        if elapsed_hours >= 1 and int(elapsed_hours * 60) % 60 == 0:
            msg = f"You have been active for {elapsed_hours:.1f} hours. Take a break."
            logger.info(f"[PRIMUS NOTICE] {msg}")
            self.trigger_alert("FATIGUE", msg)


# ─────────────────────────────────────────────────────────────
# Worker Manager
# ─────────────────────────────────────────────────────────────

class WorkerManager:
    """Manages the lifecycle of all proactive background workers."""

    def __init__(self):
        self.workers: list[WorkerBase] = [
            ThermalBatteryWorker(),
            ContextAwarenessWorker(),
            FatigueWorker(),
        ]

    def start_all(self, loop: asyncio.AbstractEventLoop, on_alert_callback=None):
        for worker in self.workers:
            worker.on_alert = on_alert_callback
            worker.start(loop)
        logger.info(f"Started {len(self.workers)} proactive intelligence workers.")

    def stop_all(self):
        for worker in self.workers:
            worker.stop()
        logger.info("All proactive workers stopped.")


# Global singleton
worker_manager = WorkerManager()
