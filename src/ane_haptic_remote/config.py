"""Settings: bot secrets from the environment, haptic wiring from JSON."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import FrozenSet, Mapping, Optional

MOTORS = ("left", "middle", "right")
TOPOLOGIES = ("auto", "three_drv", "single_drv", "simulate", "simulate_single")


class ConfigError(ValueError):
    """Raised for a missing or invalid setting."""


def _address(value: object) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except ValueError as exc:
        raise ConfigError(f"invalid I2C address: {value!r}") from exc


@dataclass(frozen=True)
class HardwareConfig:
    topology: str = "auto"
    i2c_bus: int = 1
    mux_address: int = 0x70
    drv_address: int = 0x5A
    motor_voltage_v: float = 3.0
    amplitude_floor: float = 0.30
    channels: Mapping[str, int] = field(
        default_factory=lambda: {"left": 4, "middle": 5, "right": 6}
    )
    single_mux_channel: Optional[int] = None
    taps: Mapping[str, int] = field(
        default_factory=lambda: {"left": 1, "middle": 2, "right": 3}
    )
    tap_on_ms: float = 90.0
    tap_off_ms: float = 110.0
    sector_gap_ms: float = 250.0
    group_gap_ms: float = 600.0
    default_intensity: float = 0.80
    default_auto_off_s: float = 10.0
    max_continuous_on_s: float = 60.0

    def __post_init__(self) -> None:
        if self.topology not in TOPOLOGIES:
            raise ConfigError(f"topology must be one of {TOPOLOGIES}")
        if set(self.channels) != set(MOTORS):
            raise ConfigError("channels must name exactly left, middle and right")
        if any(not 0 <= int(ch) <= 7 for ch in self.channels.values()):
            raise ConfigError("mux channels must be 0..7")
        if len(set(self.channels.values())) != len(MOTORS):
            raise ConfigError("each motor needs its own mux channel")
        if self.single_mux_channel is not None and not 0 <= self.single_mux_channel <= 7:
            raise ConfigError("single_mux_channel must be 0..7 or null")
        if set(self.taps) != set(MOTORS) or any(not 1 <= int(n) <= 6 for n in self.taps.values()):
            raise ConfigError("taps must give 1..6 taps for left, middle and right")
        if not 0.0 < self.motor_voltage_v <= 5.4:
            raise ConfigError("motor_voltage_v must be in (0, 5.4]")
        if not 0.0 <= self.amplitude_floor < 1.0:
            raise ConfigError("amplitude_floor must be in [0, 1)")
        if not 0.0 < self.default_intensity <= 1.0:
            raise ConfigError("default_intensity must be in (0, 1]")
        if self.default_auto_off_s < 0.0:
            raise ConfigError("default_auto_off_s must not be negative")
        if self.max_continuous_on_s <= 0.0:
            raise ConfigError("max_continuous_on_s must be positive")
        if min(self.tap_on_ms, self.tap_off_ms, self.sector_gap_ms, self.group_gap_ms) <= 0.0:
            raise ConfigError("tap timings must be positive")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "HardwareConfig":
        known = {item.name for item in fields(cls)}
        data = {key: value for key, value in payload.items() if key in known}
        for key in ("mux_address", "drv_address"):
            if key in data:
                data[key] = _address(data[key])
        for key in ("channels", "taps"):
            if key in data:
                data[key] = {str(name): int(value) for name, value in dict(data[key]).items()}
        if data.get("single_mux_channel") is not None:
            data["single_mux_channel"] = int(data["single_mux_channel"])
        return cls(**data)


def load_hardware_config(path: Optional[Path]) -> HardwareConfig:
    if path is None:
        return HardwareConfig()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read hardware config {path}: {exc}") from exc
    return HardwareConfig.from_dict(payload)


def parse_user_ids(raw: str) -> FrozenSet[int]:
    ids = set()
    for item in re.split(r"[\s,;]+", raw.strip()):
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_USER_IDS has a non-numeric entry: {item!r}") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class BotSettings:
    token: str
    allowed_user_ids: FrozenSet[int] = frozenset()
    topology: Optional[str] = None
    config_path: Optional[Path] = None
    default_auto_off_s: Optional[float] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, require_token: bool = True) -> "BotSettings":
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if token.startswith("paste_"):
            token = ""
        if require_token and not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set: create a bot with @BotFather and put its token in .env")
        topology = env.get("HAPTIC_TOPOLOGY", "").strip() or None
        if topology is not None and topology not in TOPOLOGIES:
            raise ConfigError(f"HAPTIC_TOPOLOGY must be one of {TOPOLOGIES}")
        config = env.get("HAPTIC_CONFIG", "").strip()
        auto_off_raw = env.get("DEFAULT_AUTO_OFF_S", "").strip()
        try:
            auto_off = float(auto_off_raw) if auto_off_raw else None
        except ValueError as exc:
            raise ConfigError("DEFAULT_AUTO_OFF_S must be a number of seconds") from exc
        return cls(
            token=token,
            allowed_user_ids=parse_user_ids(env.get("ALLOWED_USER_IDS", "")),
            topology=topology,
            config_path=Path(config) if config else None,
            default_auto_off_s=auto_off,
        )


def load_dotenv(path: Path) -> bool:
    """Load KEY=VALUE lines without overriding variables that are already set."""

    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True
