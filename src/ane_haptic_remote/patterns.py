"""Vibration patterns and timelines, free of hardware so they are easy to test.

A timeline is a tuple of (frame, seconds) steps where a frame maps a motor name
to an amplitude in 0..1; a missing motor is off.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from .config import MOTORS, HardwareConfig

Frame = Dict[str, float]
Timeline = Tuple[Tuple[Frame, float], ...]

PATTERNS = ("steady", "pulse", "heartbeat", "ramp")
SEQUENCES = ("sweep", "bounce", "sos", "test")


def pattern_gain(pattern: str, elapsed_s: float) -> float:
    t = max(0.0, elapsed_s)
    if pattern == "pulse":
        return 1.0 if (t % 0.5) < 0.25 else 0.0
    if pattern == "heartbeat":
        phase = t % 1.0
        return 1.0 if phase < 0.12 or 0.22 <= phase < 0.34 else 0.0
    if pattern == "ramp":
        phase = (t % 1.6) / 1.6
        return 0.3 + 0.7 * (1.0 - abs(2.0 * phase - 1.0))
    return 1.0


def timeline_length(timeline: Timeline) -> float:
    return sum(seconds for _, seconds in timeline)


def frame_at(timeline: Timeline, elapsed_s: float, repeat: bool = False) -> Optional[Frame]:
    """The frame playing at elapsed_s, or None once a non-repeating timeline ends."""

    length = timeline_length(timeline)
    if not timeline or length <= 0.0:
        return None
    position = max(0.0, elapsed_s)
    if repeat:
        position %= length
    elif position >= length:
        return None
    for frame, seconds in timeline:
        if position < seconds:
            return frame
        position -= seconds
    return {} if repeat else None


def build_sequence(name: str, intensity: float, repeat: Optional[int] = None) -> Timeline:
    if name not in SEQUENCES:
        raise ValueError(f"unknown sequence {name!r}")
    amplitude = max(0.0, min(1.0, float(intensity)))
    off: Frame = {}

    def on(*motors: str) -> Frame:
        return {motor: amplitude for motor in motors}

    steps: List[Tuple[Frame, float]] = []
    if name == "sweep":
        for _ in range(repeat or 2):
            for motor in MOTORS:
                steps += [(on(motor), 0.35), (off, 0.10)]
            steps.append((off, 0.30))
    elif name == "bounce":
        for _ in range(repeat or 2):
            for motor in ("left", "middle", "right", "middle"):
                steps += [(on(motor), 0.30), (off, 0.08)]
    elif name == "sos":
        dot = [(on(*MOTORS), 0.15), (off, 0.15)]
        dash = [(on(*MOTORS), 0.45), (off, 0.15)]
        for _ in range(repeat or 1):
            for letter in (dot * 3, dash * 3, dot * 3):
                steps += letter
                steps.append((off, 0.30))
    else:
        for motor in MOTORS:
            steps += [(on(motor), 0.60), (off, 0.50)]
    return tuple(steps)


def tap_cycle(active: Mapping[str, float], config: HardwareConfig) -> Timeline:
    """Direction as a tap code for one driver feeding all motors (left 1, middle 2, right 3)."""

    on_s = config.tap_on_ms / 1000.0
    off_s = config.tap_off_ms / 1000.0
    motors = [motor for motor in MOTORS if active.get(motor, 0.0) > 0.0]
    steps: List[Tuple[Frame, float]] = []
    for index, motor in enumerate(motors):
        taps = int(config.taps[motor])
        for tap in range(taps):
            steps.append(({motor: float(active[motor])}, on_s))
            if tap < taps - 1:
                steps.append(({}, off_s))
        gap = config.sector_gap_ms if index < len(motors) - 1 else config.group_gap_ms
        steps.append(({}, gap / 1000.0))
    return tuple(steps)


def encode_for_shared(timeline: Timeline, config: HardwareConfig) -> Timeline:
    """Rewrite a sequence for a single driver: one-motor steps become their tap group."""

    encoded: List[Tuple[Frame, float]] = []
    for frame, seconds in timeline:
        active = {motor: amp for motor, amp in frame.items() if amp > 0.0}
        if not active or len(active) == len(MOTORS):
            encoded.append((frame, seconds))
            continue
        group = list(tap_cycle(active, config))
        group[-1] = ({}, config.sector_gap_ms / 1000.0)
        encoded.extend(group)
    return tuple(encoded)
