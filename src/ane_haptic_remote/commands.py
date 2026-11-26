"""Turn what the user types ("left 80 5s", "sweep", "stop") into a command."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import MOTORS

MOTOR_WORDS = {
    "left": ("left",), "l": ("left",), "lft": ("left",),
    "middle": ("middle",), "mid": ("middle",), "center": ("middle",), "centre": ("middle",),
    "m": ("middle",), "c": ("middle",), "front": ("middle",), "forward": ("middle",), "straight": ("middle",),
    "right": ("right",), "r": ("right",), "rt": ("right",),
    "both": ("left", "right"), "sides": ("left", "right"),
    "all": MOTORS, "every": MOTORS, "everything": MOTORS, "three": MOTORS,
}
EMOJI = {
    "\u2b05\ufe0f": "left", "\u2b05": "left", "\U0001f448": "left",
    "\u27a1\ufe0f": "right", "\u27a1": "right", "\U0001f449": "right",
    "\u2b06\ufe0f": "middle", "\u2b06": "middle", "\U0001f446": "middle", "\u23fa": "middle",
    "\u23f9": "stop", "\U0001f6d1": "stop",
}
INTENSITY_WORDS = {
    "soft": 0.4, "low": 0.4, "gentle": 0.4, "weak": 0.4, "light": 0.4,
    "medium": 0.7, "med": 0.7, "normal": 0.7,
    "high": 0.9, "strong": 1.0, "hard": 1.0, "max": 1.0, "full": 1.0,
}
PATTERN_WORDS = {
    "steady": "steady", "solid": "steady", "constant": "steady", "continuous": "steady",
    "pulse": "pulse", "pulsing": "pulse", "blink": "pulse",
    "heartbeat": "heartbeat", "heart": "heartbeat", "beat": "heartbeat",
    "ramp": "ramp", "breathe": "ramp", "breathing": "ramp",
}
SEQUENCE_WORDS = {
    "sweep": "sweep", "scan": "sweep", "bounce": "bounce", "pingpong": "bounce", "wave": "bounce",
    "sos": "sos", "test": "test", "check": "test",
}
STOP_WORDS = {"stop", "off", "halt", "quiet", "silence", "cancel", "enough", "kill"}
HOLD_WORDS = {"hold", "forever", "on", "keep", "nonstop"}
SPECIAL_WORDS = {
    "status": "status", "state": "status",
    "help": "help", "commands": "help",
    "remote": "panel", "panel": "panel", "menu": "panel", "controls": "panel", "keyboard": "panel",
    "whoami": "whoami", "id": "whoami",
    "hardware": "hardware", "hw": "hardware", "wiring": "hardware",
}

_UNIT = r"(?:(?:minutes|minute|mins|min|msec|ms|seconds|second|secs|sec|s|percent)\b|%)"
TOKEN_RE = re.compile(r"\d+(?:\.\d+)?\s*" + _UNIT + r"?|[a-z]+")
NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + _UNIT + r")?")


@dataclass(frozen=True)
class Command:
    action: str  # hold | stop | sequence | status | help | panel | whoami | hardware
    motors: Tuple[str, ...] = ()
    intensity: Optional[float] = None
    duration_s: Optional[float] = None  # None: default auto-off, 0: until stopped
    pattern: Optional[str] = None
    sequence: Optional[str] = None


def _ordered(motors: List[str]) -> Tuple[str, ...]:
    return tuple(motor for motor in MOTORS if motor in motors)


def parse_command(text: Optional[str]) -> Optional[Command]:
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    for emoji, word in EMOJI.items():
        cleaned = cleaned.replace(emoji, f" {word} ")
    cleaned = re.sub(r"^/(\w+)(@\w+)?", r"\1", cleaned.strip().lower())

    motors: List[str] = []
    intensity: Optional[float] = None
    duration: Optional[float] = None
    pattern: Optional[str] = None
    sequence: Optional[str] = None
    special: Optional[str] = None
    stop = False
    recognized = False

    for raw in TOKEN_RE.findall(cleaned):
        token = raw.strip()
        number = NUMBER_RE.fullmatch(token)
        if number:
            value = float(number.group(1))
            unit = (number.group(2) or "").strip()
            if unit in ("ms", "msec"):
                duration = value / 1000.0
            elif unit in ("min", "mins", "minute", "minutes"):
                duration = value * 60.0
            elif unit in ("s", "sec", "secs", "second", "seconds"):
                duration = value
            elif unit in ("%", "percent"):
                intensity = value / 100.0
            else:
                intensity = value / 100.0 if value > 1.0 else value
            recognized = True
            continue
        if token in STOP_WORDS:
            stop = True
        elif token in MOTOR_WORDS:
            motors.extend(motor for motor in MOTOR_WORDS[token] if motor not in motors)
        elif token in INTENSITY_WORDS:
            intensity = INTENSITY_WORDS[token]
        elif token in PATTERN_WORDS:
            pattern = PATTERN_WORDS[token]
        elif token in SEQUENCE_WORDS:
            sequence = SEQUENCE_WORDS[token]
        elif token in HOLD_WORDS:
            duration = 0.0
        elif token in SPECIAL_WORDS:
            special = SPECIAL_WORDS[token]
        else:
            continue
        recognized = True

    if not recognized:
        return None
    if intensity is not None:
        intensity = max(0.0, min(1.0, intensity))
    if stop:
        return Command("stop", _ordered(motors))
    if sequence:
        return Command("sequence", intensity=intensity, sequence=sequence)
    if motors:
        return Command("hold", _ordered(motors), intensity, duration, pattern)
    if special:
        return Command(special)
    return None
