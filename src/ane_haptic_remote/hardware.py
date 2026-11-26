"""I2C access, the DRV2605L driver, wiring detection, and motor backends.

Two wirings are supported:

* three_drv: one DRV2605L per motor behind a TCA9548A mux, so left, middle and
  right vibrate independently;
* single_drv: one DRV2605L driving all three ERMs in parallel (the current
  cane harness), so direction is played as a tap code.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from .config import MOTORS, HardwareConfig

LOGGER = logging.getLogger("ane_haptic_remote.hardware")

REG_STATUS = 0x00
REG_MODE = 0x01
REG_RTP = 0x02
REG_OD_CLAMP = 0x17
REG_FEEDBACK = 0x1A
REG_CONTROL3 = 0x1D
MODE_INTERNAL_TRIGGER = 0x00
MODE_RTP = 0x05
MODE_STANDBY = 0x40
MODE_RESET = 0x80
FEEDBACK_N_ERM_LRA = 0x80
CONTROL3_ERM_OPEN_LOOP = 0x20
CONTROL3_RTP_UNSIGNED = 0x08
RTP_FULL_SCALE = 127
OD_CLAMP_VOLTS_PER_LSB = 0.02118
KNOWN_DEVICE_IDS = {3, 4, 6, 7}


class I2CBus(Protocol):
    def read_byte(self, address: int) -> int: ...

    def read_byte_data(self, address: int, register: int) -> int: ...

    def write_byte(self, address: int, value: int) -> None: ...

    def write_byte_data(self, address: int, register: int, value: int) -> None: ...

    def close(self) -> None: ...


class HardwareNotFound(RuntimeError):
    """No DRV2605L answered where the configuration expects one."""


def open_smbus(bus_number: int) -> I2CBus:
    try:
        from smbus2 import SMBus
    except ImportError as exc:
        raise RuntimeError("smbus2 is not installed; on the Pi run: .venv/bin/pip install -e '.[pi]'") from exc
    return SMBus(bus_number)


def od_clamp_for(voltage: float) -> int:
    return max(0, min(255, int(round(voltage / OD_CLAMP_VOLTS_PER_LSB))))


def intensity_to_rtp(intensity: float, floor: float) -> int:
    """0 stays off; anything else is lifted above the floor where an ERM starts spinning."""

    if intensity <= 0.0:
        return 0
    level = floor + (1.0 - floor) * min(float(intensity), 1.0)
    return max(1, min(RTP_FULL_SCALE, int(round(level * RTP_FULL_SCALE))))


class Mux:
    def __init__(self, bus: I2CBus, address: int) -> None:
        self._bus = bus
        self.address = address
        self._active: Optional[int] = None

    def select(self, channel: Optional[int]) -> None:
        value = 0 if channel is None else 1 << channel
        if value == self._active:
            return
        try:
            self._bus.write_byte(self.address, value)
        except Exception:
            self._active = None
            raise
        self._active = value


class Drv2605:
    """DRV2605L as an open-loop ERM driver in real-time-playback mode."""

    def __init__(
        self,
        bus: I2CBus,
        address: int,
        channel: Optional[int],
        mux: Optional[Mux],
        lock: threading.RLock,
        motor_voltage_v: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bus = bus
        self.address = address
        self.channel = channel
        self._mux = mux
        self._lock = lock
        self._voltage = motor_voltage_v
        self._sleep = sleep
        self.device_id: Optional[int] = None

    def _read(self, register: int) -> int:
        with self._lock:
            if self._mux is not None:
                self._mux.select(self.channel)
            return self._bus.read_byte_data(self.address, register)

    def _write(self, register: int, value: int) -> None:
        with self._lock:
            if self._mux is not None:
                self._mux.select(self.channel)
            self._bus.write_byte_data(self.address, register, value & 0xFF)

    def configure(self) -> None:
        self.device_id = self._read(REG_STATUS) >> 5
        if self.device_id not in KNOWN_DEVICE_IDS:
            LOGGER.warning("unexpected DRV2605 device id %s on channel %s", self.device_id, self.channel)
        self._write(REG_MODE, MODE_RESET)
        self._sleep(0.02)
        self._write(REG_MODE, MODE_INTERNAL_TRIGGER)
        self._write(REG_FEEDBACK, self._read(REG_FEEDBACK) & ~FEEDBACK_N_ERM_LRA)
        control3 = self._read(REG_CONTROL3)
        self._write(REG_CONTROL3, (control3 | CONTROL3_ERM_OPEN_LOOP) & ~CONTROL3_RTP_UNSIGNED)
        self._write(REG_OD_CLAMP, od_clamp_for(self._voltage))
        self._write(REG_RTP, 0)
        self._write(REG_MODE, MODE_RTP)

    def set_rtp(self, value: int) -> None:
        self._write(REG_RTP, max(0, min(RTP_FULL_SCALE, int(value))))

    def stop(self) -> None:
        self._write(REG_RTP, 0)

    def standby(self) -> None:
        self._write(REG_RTP, 0)
        self._write(REG_MODE, MODE_STANDBY)


@dataclass(frozen=True)
class Wiring:
    topology: str
    mux_present: bool
    channels: Mapping[str, Optional[int]]

    def describe(self, config: HardwareConfig) -> str:
        if self.topology == "three_drv":
            where = ", ".join(f"{motor} CH{self.channels[motor]}" for motor in MOTORS)
            return f"3x DRV2605L, independent motors, behind TCA9548A {hex(config.mux_address)}: {where}"
        if self.topology == "single_drv":
            channel = self.channels.get("all")
            where = "main I2C bus" if channel is None else f"mux CH{channel}"
            taps = ", ".join(f"{motor} {config.taps[motor]}" for motor in MOTORS)
            return (
                f"1x DRV2605L at {hex(config.drv_address)} on the {where} driving all 3 motors together; "
                f"direction is a tap code ({taps})"
            )
        if self.topology == "simulate_single":
            return "Simulated single driver (tap code), no hardware"
        return "Simulated independent motors, no hardware"


def _responds(probe: Callable[[], object]) -> bool:
    try:
        probe()
        return True
    except OSError:
        return False


def scan_bus(bus: I2CBus, config: HardwareConfig) -> Dict[str, List[int]]:
    """Addresses answering on the main bus and, behind the mux, on each channel."""

    def sweep(skip: Tuple[int, ...] = ()) -> List[int]:
        return [
            address
            for address in range(0x03, 0x78)
            if address not in skip and _responds(lambda address=address: bus.read_byte(address))
        ]

    mux_present = _responds(lambda: bus.read_byte(config.mux_address))
    if mux_present:
        bus.write_byte(config.mux_address, 0)
    report = {"main": sweep()}
    if mux_present:
        hidden = tuple(report["main"])
        for channel in range(8):
            bus.write_byte(config.mux_address, 1 << channel)
            report[f"ch{channel}"] = sweep(hidden)
        bus.write_byte(config.mux_address, 0)
    return report


def detect_wiring(bus: I2CBus, config: HardwareConfig, topology: str = "auto") -> Wiring:
    mux_present = _responds(lambda: bus.read_byte(config.mux_address))

    def drv_on(channel: Optional[int]) -> bool:
        if channel is not None:
            if not mux_present:
                return False
            bus.write_byte(config.mux_address, 1 << channel)
        elif mux_present:
            bus.write_byte(config.mux_address, 0)
        return _responds(lambda: bus.read_byte_data(config.drv_address, REG_STATUS))

    try:
        if topology in ("auto", "three_drv") and mux_present and not drv_on(None):
            found = {motor: ch for motor, ch in config.channels.items() if drv_on(ch)}
            if len(found) == len(MOTORS):
                return Wiring("three_drv", True, found)
        if topology == "three_drv":
            raise HardwareNotFound(
                "three_drv needs a DRV2605L on mux channels "
                + ", ".join(f"{m} CH{c}" for m, c in config.channels.items())
            )
        if config.single_mux_channel is not None:
            candidates: List[Optional[int]] = [config.single_mux_channel]
        else:
            candidates = [None] + (list(range(8)) if mux_present else [])
        for channel in candidates:
            if drv_on(channel):
                return Wiring("single_drv", mux_present, {"all": channel})
    finally:
        if mux_present:
            try:
                bus.write_byte(config.mux_address, 0)
            except OSError:
                pass
    raise HardwareNotFound(
        f"no DRV2605L answered at {hex(config.drv_address)}; run 'ane-remote scan' and check I2C wiring"
    )


class _DriverSet:
    independent = True

    def __init__(self, drivers: Dict[str, Drv2605], floor: float, bus: Optional[I2CBus]) -> None:
        self._drivers = drivers
        self._floor = floor
        self._bus = bus
        self._last: Dict[str, Optional[int]] = {key: None for key in drivers}
        self._consecutive: Dict[str, int] = {key: 0 for key in drivers}
        self._last_reconfigure: Dict[str, float] = {key: 0.0 for key in drivers}
        self.error_count = 0
        self.write_count = 0

    def _set(self, key: str, intensity: float) -> None:
        value = intensity_to_rtp(intensity, self._floor)
        if value == self._last[key]:
            return
        try:
            self._drivers[key].set_rtp(value)
        except Exception as exc:
            self.error_count += 1
            self._consecutive[key] += 1
            self._last[key] = None
            if self._consecutive[key] in (1, 10, 100):
                LOGGER.error("haptic write to %s failed (%d in a row): %s", key, self._consecutive[key], exc)
            self._maybe_reconfigure(key)
            return
        self._last[key] = value
        self._consecutive[key] = 0
        self.write_count += 1

    def _maybe_reconfigure(self, key: str) -> None:
        now = time.monotonic()
        if self._consecutive[key] < 3 or now - self._last_reconfigure[key] < 1.0:
            return
        self._last_reconfigure[key] = now
        try:
            self._drivers[key].configure()
            LOGGER.warning("re-initialised DRV2605L %s", key)
        except Exception as exc:
            LOGGER.error("could not re-initialise DRV2605L %s: %s", key, exc)

    def stop_all(self) -> None:
        for key, driver in self._drivers.items():
            for attempt in range(3):
                try:
                    driver.stop()
                    self._last[key] = 0
                    break
                except Exception as exc:
                    if attempt == 2:
                        LOGGER.error("could not stop motor %s: %s", key, exc)
                    else:
                        time.sleep(0.02)

    def close(self) -> None:
        self.stop_all()
        for driver in self._drivers.values():
            try:
                driver.standby()
            except Exception:
                pass
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass

    def health(self) -> Dict[str, object]:
        return {"writes": self.write_count, "errors": self.error_count}


class IndependentDrvBackend(_DriverSet):
    independent = True

    def write(self, levels: Mapping[str, float]) -> None:
        for motor in MOTORS:
            self._set(motor, levels.get(motor, 0.0))


class SharedDrvBackend(_DriverSet):
    independent = False

    def write(self, levels: Mapping[str, float]) -> None:
        self._set("all", max(levels.values(), default=0.0))


class SimulatedBackend:
    """In-memory motors for laptops and tests; optional callback on every change."""

    def __init__(
        self,
        independent: bool = True,
        on_change: Optional[Callable[[Dict[str, float]], None]] = None,
    ) -> None:
        self.independent = independent
        self.levels: Dict[str, float] = {motor: 0.0 for motor in MOTORS}
        self.changes: List[Dict[str, float]] = []
        self.closed = False
        self._on_change = on_change

    def write(self, levels: Mapping[str, float]) -> None:
        new = {motor: round(float(levels.get(motor, 0.0)), 3) for motor in MOTORS}
        if not self.independent:
            peak = max(new.values())
            new = {motor: peak for motor in MOTORS}
        if new != self.levels:
            self.levels = new
            self.changes.append(dict(new))
            if self._on_change is not None:
                self._on_change(dict(new))

    def stop_all(self) -> None:
        self.write({})

    def close(self) -> None:
        self.stop_all()
        self.closed = True

    def health(self) -> Dict[str, object]:
        return {"simulated": True, "changes": len(self.changes)}


def build_backend(
    config: HardwareConfig,
    topology: str,
    bus_factory: Callable[[int], I2CBus] = open_smbus,
    on_change: Optional[Callable[[Dict[str, float]], None]] = None,
) -> Tuple[object, Wiring]:
    if topology in ("simulate", "simulate_single"):
        independent = topology == "simulate"
        return SimulatedBackend(independent, on_change), Wiring(topology, False, {})
    bus = bus_factory(config.i2c_bus)
    try:
        wiring = detect_wiring(bus, config, topology)
        lock = threading.RLock()
        mux = Mux(bus, config.mux_address) if wiring.mux_present else None
        drivers = {
            key: Drv2605(bus, config.drv_address, channel, mux, lock, config.motor_voltage_v)
            for key, channel in wiring.channels.items()
        }
        for driver in drivers.values():
            driver.configure()
        backend_cls = IndependentDrvBackend if wiring.topology == "three_drv" else SharedDrvBackend
        return backend_cls(drivers, config.amplitude_floor, bus), wiring
    except BaseException:
        bus.close()
        raise
