"""Owns the motors: turns hold and sequence requests into amplitudes 200 times a second.

Every hold has an end time (the requested duration, never more than
max_continuous_on_s), so a lost Telegram connection can never leave a motor
running. close() always parks the motors.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional, Tuple

from .config import MOTORS, HardwareConfig
from .patterns import (
    PATTERNS,
    Timeline,
    build_sequence,
    encode_for_shared,
    frame_at,
    pattern_gain,
    tap_cycle,
    timeline_length,
)

LOGGER = logging.getLogger("ane_haptic_remote.controller")


@dataclass(frozen=True)
class Hold:
    intensity: float
    pattern: str
    started: float
    until: float


@dataclass(frozen=True)
class HoldResult:
    motors: Tuple[str, ...]
    intensity: float
    duration_s: float
    until_stopped: bool


@dataclass(frozen=True)
class _Sequence:
    name: str
    timeline: Timeline
    started: float
    length_s: float


class HapticController:
    def __init__(
        self,
        backend: object,
        config: HardwareConfig,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = 0.005,
    ) -> None:
        self.backend = backend
        self.config = config
        self._clock = clock
        self._tick_s = tick_s
        self._lock = threading.RLock()
        self._holds: Dict[str, Hold] = {}
        self._sequence: Optional[_Sequence] = None
        self._shared_start = 0.0
        self._shared_key: Optional[Tuple[Tuple[str, float], ...]] = None
        self._shared_cycle: Timeline = ()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.write_errors = 0

    @property
    def independent(self) -> bool:
        return bool(getattr(self.backend, "independent", True))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="haptic-controller", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            self._holds.clear()
            self._sequence = None
        for action in (self.backend.stop_all, self.backend.close):
            try:
                action()
            except Exception as exc:
                LOGGER.error("motor shutdown step failed: %s", exc)

    # -- requests ----------------------------------------------------------

    def hold(
        self,
        motors: Iterable[str],
        intensity: Optional[float] = None,
        duration_s: Optional[float] = None,
        pattern: str = "steady",
    ) -> HoldResult:
        wanted = set(motors)
        chosen = tuple(motor for motor in MOTORS if motor in wanted)
        if not chosen:
            raise ValueError("no motor selected")
        if pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {pattern!r}")
        level = self.config.default_intensity if intensity is None else float(intensity)
        level = min(1.0, max(0.0, level))
        if level <= 0.0:
            self.stop(chosen)
            return HoldResult(chosen, 0.0, 0.0, False)
        requested = self.config.default_auto_off_s if duration_s is None else float(duration_s)
        until_stopped = requested <= 0.0
        cap = self.config.max_continuous_on_s
        duration = cap if until_stopped else min(requested, cap)
        now = self._clock()
        with self._lock:
            self._sequence = None
            for motor in chosen:
                self._holds[motor] = Hold(level, pattern, now, now + duration)
            self._shared_start = now
        self._wake.set()
        LOGGER.info("hold %s at %.0f%% for %.1fs (%s)", "+".join(chosen), level * 100, duration, pattern)
        return HoldResult(chosen, level, duration, until_stopped)

    def play(self, name: str, intensity: Optional[float] = None, repeat: Optional[int] = None) -> float:
        level = self.config.default_intensity if intensity is None else min(1.0, max(0.0, float(intensity)))
        timeline = build_sequence(name, level, repeat)
        if not self.independent:
            timeline = encode_for_shared(timeline, self.config)
        length = timeline_length(timeline)
        with self._lock:
            self._holds.clear()
            self._sequence = _Sequence(name, timeline, self._clock(), length)
        self._wake.set()
        LOGGER.info("sequence %s at %.0f%% (%.1fs)", name, level * 100, length)
        return length

    def retune(self, intensity: Optional[float] = None, pattern: Optional[str] = None) -> Tuple[str, ...]:
        """Change the strength or pattern of whatever is already humming, keeping its end time and phase."""

        if pattern is not None and pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {pattern!r}")
        level = None if intensity is None else min(1.0, max(0.0, float(intensity)))
        with self._lock:
            for motor, hold in list(self._holds.items()):
                self._holds[motor] = Hold(
                    hold.intensity if level is None else level,
                    hold.pattern if pattern is None else pattern,
                    hold.started,
                    hold.until,
                )
            live = tuple(motor for motor in MOTORS if motor in self._holds)
        if live:
            self._wake.set()
        return live

    def stop(self, motors: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
        with self._lock:
            if motors is None:
                stopped = tuple(motor for motor in MOTORS if motor in self._holds)
                self._holds.clear()
            else:
                wanted = set(motors)
                stopped = tuple(motor for motor in MOTORS if motor in wanted and self._holds.pop(motor, None))
            self._sequence = None
        self._wake.set()
        LOGGER.info("stop %s", "all" if motors is None else "+".join(stopped) or "nothing")
        return stopped

    # -- rendering -----------------------------------------------------------

    def _expire(self, now: float) -> None:
        for motor, hold in list(self._holds.items()):
            if now >= hold.until:
                del self._holds[motor]
                LOGGER.info("auto-off %s", motor)

    def render(self, now: float) -> Dict[str, float]:
        """Amplitude for every motor at time now."""

        with self._lock:
            self._expire(now)
            sequence = self._sequence
            if sequence is not None:
                frame = frame_at(sequence.timeline, now - sequence.started)
                if frame is None:
                    self._sequence = None
                else:
                    return {motor: float(frame.get(motor, 0.0)) for motor in MOTORS}
            levels = {motor: 0.0 for motor in MOTORS}
            if not self._holds:
                return levels
            if self.independent:
                for motor, hold in self._holds.items():
                    levels[motor] = hold.intensity * pattern_gain(hold.pattern, now - hold.started)
                return levels
            active = {motor: hold.intensity for motor, hold in self._holds.items()}
            key = tuple(sorted(active.items()))
            if key != self._shared_key:
                self._shared_key = key
                self._shared_cycle = tap_cycle(active, self.config)
                self._shared_start = now
            frame = frame_at(self._shared_cycle, now - self._shared_start, repeat=True) or {}
            for motor in MOTORS:
                levels[motor] = float(frame.get(motor, 0.0))
            return levels

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        now = self._clock() if now is None else now
        with self._lock:
            self._expire(now)
            motors = {}
            for motor in MOTORS:
                hold = self._holds.get(motor)
                motors[motor] = {
                    "active": hold is not None,
                    "intensity": hold.intensity if hold else 0.0,
                    "pattern": hold.pattern if hold else None,
                    "remaining_s": max(0.0, hold.until - now) if hold else 0.0,
                }
            sequence = None
            if self._sequence is not None:
                remaining = self._sequence.length_s - (now - self._sequence.started)
                if remaining > 0:
                    sequence = {"name": self._sequence.name, "remaining_s": remaining}
            return {"independent": self.independent, "motors": motors, "sequence": sequence}

    def _run(self) -> None:
        while not self._stop.is_set():
            levels = self.render(self._clock())
            try:
                self.backend.write(levels)
            except Exception as exc:
                self.write_errors += 1
                if self.write_errors in (1, 10, 100):
                    LOGGER.error("motor write failed (%d): %s", self.write_errors, exc)
            self._wake.wait(self._tick_s)
            self._wake.clear()
