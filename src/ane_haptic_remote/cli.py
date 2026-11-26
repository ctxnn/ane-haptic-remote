"""ane-remote: run the Telegram bot, scan the I2C bus, or test the motors locally."""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from .bot import RemoteBot
from .commands import parse_command
from .config import (
    MOTORS,
    TOPOLOGIES,
    BotSettings,
    ConfigError,
    HardwareConfig,
    load_dotenv,
    load_hardware_config,
)
from .controller import HapticController
from .hardware import (
    HardwareNotFound,
    build_backend,
    detect_wiring,
    open_smbus,
    scan_bus,
)
from .telegram_api import TelegramAPI, TelegramError

LOGGER = logging.getLogger("ane_haptic_remote")


class _Shutdown(Exception):
    pass


def _print_levels(levels: Dict[str, float]) -> None:
    parts = []
    for motor in MOTORS:
        level = levels[motor]
        filled = round(level * 8)
        parts.append(f"{motor[0].upper()} {'#' * filled}{'.' * (8 - filled)}")
    print(f"{time.monotonic():10.2f}  " + "   ".join(parts), flush=True)


def _cane_service_active() -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "yolo-pi-cane"], timeout=3, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_bot(settings: BotSettings, config: HardwareConfig, topology: str) -> int:
    if _cane_service_active():
        LOGGER.warning(
            "yolo-pi-cane is running and also drives the DRV2605L; stop it first: sudo systemctl stop yolo-pi-cane"
        )
    backend, wiring = build_backend(config, topology)
    controller = HapticController(backend, config)
    controller.start()
    stop_event = threading.Event()

    def _handle(_signum: int, _frame: object) -> None:
        stop_event.set()
        controller.stop()
        raise _Shutdown()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _handle)
    try:
        api = TelegramAPI(settings.token)
        me = api.get_me()
        LOGGER.info("bot @%s ready; hardware: %s", me.get("username"), wiring.describe(config))
        if not settings.allowed_user_ids:
            LOGGER.warning("ALLOWED_USER_IDS is empty, so the remote is LOCKED: send /whoami to the bot and add your ID")
        RemoteBot(api, controller, settings, wiring.describe(config)).run_forever(stop_event)
    except _Shutdown:
        LOGGER.info("shutting down")
    except TelegramError as exc:
        LOGGER.error("Telegram rejected the bot: %s", exc)
        return 1
    finally:
        controller.close()
    return 0


def _scan(config: HardwareConfig) -> int:
    bus = open_smbus(config.i2c_bus)
    try:
        report = scan_bus(bus, config)
        for where, addresses in report.items():
            listed = " ".join(hex(address) for address in addresses) or "-"
            print(f"{where:>5}: {listed}")
        try:
            wiring = detect_wiring(bus, config, "auto")
            print(f"\ndetected: {wiring.describe(config)}")
        except HardwareNotFound as exc:
            print(f"\n{exc}")
            return 1
    finally:
        bus.close()
    return 0


def _test(words: List[str], config: HardwareConfig, topology: str) -> int:
    command = parse_command(" ".join(words) or "test")
    if command is None or command.action not in ("hold", "sequence"):
        print("try: ane-remote test left 80 2s | right pulse | sweep | sos", file=sys.stderr)
        return 2
    on_change = _print_levels if topology.startswith("simulate") else None
    backend, wiring = build_backend(config, topology, on_change=on_change)
    print(f"hardware: {wiring.describe(config)}")
    controller = HapticController(backend, config)
    controller.start()
    try:
        if command.action == "sequence":
            length = controller.play(command.sequence, command.intensity)
        else:
            duration = command.duration_s if command.duration_s else 2.0
            length = controller.hold(command.motors, command.intensity, duration, command.pattern or "steady").duration_s
        time.sleep(length + 0.1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ane-remote", description=__doc__)
    parser.add_argument("--env", default=".env", help="dotenv file to load (default: .env)")
    parser.add_argument("--config", default=None, help="hardware JSON (default: HAPTIC_CONFIG or auto-detect)")
    parser.add_argument("--topology", choices=TOPOLOGIES, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the Telegram bot")
    sub.add_parser("scan", help="list I2C devices and the detected haptic wiring")
    test = sub.add_parser("test", help="play a command on the motors without Telegram")
    test.add_argument("words", nargs="*", help="e.g. left 80 2s, sweep, sos")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(Path(args.env))
    try:
        settings = BotSettings.from_env(require_token=args.command == "run")
        config = load_hardware_config(Path(args.config) if args.config else settings.config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if settings.default_auto_off_s is not None:
        config = replace(config, default_auto_off_s=settings.default_auto_off_s)
    topology = args.topology or settings.topology or config.topology

    try:
        if args.command == "run":
            return _run_bot(settings, config, topology)
        if args.command == "scan":
            return _scan(config)
        return _test(args.words, config, topology)
    except (HardwareNotFound, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
