"""The Telegram front end: a live inline remote panel plus plain-text commands."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .commands import Command, parse_command
from .config import MOTORS, BotSettings
from .controller import HapticController
from .patterns import PATTERNS
from .telegram_api import TelegramError

LOGGER = logging.getLogger("ane_haptic_remote.bot")

NAMES = {"left": "\u2b05\ufe0f Left", "middle": "\u23fa Middle", "right": "\u27a1\ufe0f Right"}
BUTTONS = {"left": "\u2b05\ufe0f Left", "middle": "\u23fa Middle", "right": "Right \u27a1\ufe0f"}
SEQUENCE_NAMES = {
    "sweep": "\u2194\ufe0f Sweep",
    "bounce": "\U0001f501 Bounce",
    "sos": "\U0001f198 SOS",
    "test": "\U0001f9ea Test",
}
PATTERN_NAMES = {"steady": "Steady", "pulse": "Pulse", "heartbeat": "Heartbeat", "ramp": "Ramp"}
DURATION_CHOICES = (3.0, 10.0, 30.0, 0.0)
INTENSITY_STEP = 0.1
ON = "\U0001f7e2"

BOT_COMMANDS = [
    ("remote", "Open the live remote panel"),
    ("left", "Buzz the left motor"),
    ("middle", "Buzz the middle motor"),
    ("right", "Buzz the right motor"),
    ("all", "Buzz all three motors"),
    ("sweep", "Left, middle, right in turn"),
    ("sos", "SOS on all motors"),
    ("stop", "Stop everything now"),
    ("status", "What is buzzing right now"),
    ("hardware", "Detected haptic wiring"),
    ("whoami", "Show your Telegram user ID"),
    ("help", "How to use the remote"),
]

HELP_TEXT = (
    "<b>\U0001f9af Cane Haptic Remote</b>\n\n"
    "Tap /remote for the button panel, or just type:\n"
    "\u2022 <code>left</code> \u00b7 <code>middle</code> \u00b7 <code>right</code> \u00b7 <code>all</code>\n"
    "\u2022 <code>right 60%</code> \u00b7 <code>left 5s</code> \u00b7 <code>middle max 500ms</code>\n"
    "\u2022 <code>left and right soft</code> \u00b7 <code>middle hold</code> (until stop, 60 s cap)\n"
    "\u2022 <code>left pulse</code> \u00b7 <code>right heartbeat</code> \u00b7 <code>all ramp</code>\n"
    "\u2022 <code>sweep</code> \u00b7 <code>bounce</code> \u00b7 <code>sos</code> \u00b7 <code>test</code>\n"
    "\u2022 <code>stop</code> \u00b7 <code>stop left</code> \u00b7 <code>status</code>\n\n"
    "Every buzz switches itself off (default 10 s, never more than 60 s)."
)


def _pct(value: float) -> str:
    return f"{round(value * 100):d}%"


def _duration(value: float) -> str:
    return "hold" if value <= 0 else f"{value:g}s"


def _bar(value: float) -> str:
    filled = max(0, min(5, round(value * 5)))
    return "\u25b0" * filled + "\u25b1" * (5 - filled)


@dataclass
class PanelState:
    intensity: float
    duration_s: float
    pattern: str = "steady"


class RemoteBot:
    def __init__(
        self,
        api: Any,
        controller: HapticController,
        settings: BotSettings,
        hardware_text: str,
        poll_timeout_s: int = 20,
    ) -> None:
        self.api = api
        self.controller = controller
        self.settings = settings
        self.hardware_text = hardware_text
        self.poll_timeout_s = poll_timeout_s
        self._panels: Dict[int, PanelState] = {}

    # -- polling -------------------------------------------------------------

    def run_forever(self, stop_event: threading.Event) -> None:
        try:
            self.api.set_my_commands(BOT_COMMANDS)
        except (TelegramError, OSError) as exc:
            LOGGER.warning("could not register the command menu: %s", exc)
        offset = self._skip_pending()
        backoff = 1.0
        while not stop_event.is_set():
            try:
                updates = self.api.get_updates(offset=offset, timeout=self.poll_timeout_s)
            except TelegramError as exc:
                if exc.error_code == 401:
                    raise
                LOGGER.warning("Telegram error: %s", exc)
                stop_event.wait(exc.retry_after or backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            except OSError as exc:
                LOGGER.warning("network error: %s", exc)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            for update in updates or []:
                offset = int(update["update_id"]) + 1
                try:
                    self.handle_update(update)
                except Exception:
                    LOGGER.exception("failed to handle an update")

    def _skip_pending(self) -> Optional[int]:
        """Commands sent while the bot was offline must not buzz the moment it starts."""

        try:
            pending = self.api.get_updates(offset=-1, timeout=0)
        except (TelegramError, OSError) as exc:
            LOGGER.warning("could not skip pending updates: %s", exc)
            return None
        if pending:
            LOGGER.info("ignored commands sent while the bot was offline")
            return int(pending[-1]["update_id"]) + 1
        return None

    # -- dispatch ------------------------------------------------------------

    def handle_update(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
            return
        message = update.get("message")
        if message and isinstance(message.get("text"), str):
            self._on_message(message)

    def _authorized(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.settings.allowed_user_ids

    def _send(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.api.send_message(chat_id, text, reply_markup=reply_markup)
        except (TelegramError, OSError) as exc:
            LOGGER.warning("sendMessage failed: %s", exc)

    def _on_message(self, message: Dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user = message.get("from") or {}
        user_id = user.get("id")
        text = message["text"].strip()
        word = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

        if word in ("/whoami", "/id"):
            self._send(chat_id, f"Your Telegram user ID is <code>{user_id}</code>.")
            return
        if not self._authorized(user_id):
            LOGGER.warning("refused user %s (@%s): %r", user_id, user.get("username"), text)
            self._send(
                chat_id,
                "\U0001f512 This remote is locked.\n\n"
                f"Your Telegram user ID is <code>{user_id}</code>. Add it to "
                "<code>ALLOWED_USER_IDS</code> in the bot's <code>.env</code> on the Pi and restart the bot.",
            )
            return
        LOGGER.info("user %s (@%s): %r", user_id, user.get("username"), text)
        if word in ("/start", "/remote", "/panel"):
            self._send_panel(chat_id)
            return
        if word == "/help":
            self._send(chat_id, HELP_TEXT)
            return
        command = parse_command(text)
        if command is None:
            self._send(
                chat_id,
                "I didn't catch that. Try <code>left</code>, <code>right 60% 3s</code>, "
                "<code>sweep</code>, <code>stop</code>, or open /remote.",
            )
            return
        if command.action == "panel":
            self._send_panel(chat_id)
            return
        self._send(chat_id, self.execute(command, chat_id, user_id))

    def execute(self, command: Command, chat_id: int, user_id: Optional[int] = None) -> str:
        state = self._panel(chat_id)
        if command.action == "hold":
            result = self.controller.hold(
                command.motors,
                intensity=command.intensity if command.intensity is not None else state.intensity,
                duration_s=command.duration_s if command.duration_s is not None else state.duration_s,
                pattern=command.pattern or state.pattern,
            )
            names = " + ".join(NAMES[motor] for motor in result.motors)
            if result.intensity <= 0.0:
                return f"\u23f9 {names} off"
            length = f"until stop ({result.duration_s:g}s cap)" if result.until_stopped else f"{result.duration_s:g}s"
            extra = ""
            if self.controller.independent and (command.pattern or state.pattern) != "steady":
                extra = f" \u00b7 {PATTERN_NAMES[command.pattern or state.pattern]}"
            if not self.controller.independent:
                taps = ", ".join(f"{motor} {self.controller.config.taps[motor]}" for motor in result.motors)
                extra = f" \u00b7 tap code ({taps})"
            return f"{ON} {names} on \u00b7 {_pct(result.intensity)} \u00b7 {length}{extra}"
        if command.action == "stop":
            if command.motors:
                self.controller.stop(command.motors)
                return "\u23f9 " + " + ".join(NAMES[motor] for motor in command.motors) + " off"
            self.controller.stop()
            return "\u23f9 All motors stopped"
        if command.action == "sequence":
            intensity = command.intensity if command.intensity is not None else state.intensity
            length = self.controller.play(command.sequence, intensity)
            return f"{SEQUENCE_NAMES[command.sequence]} \u00b7 {_pct(intensity)} \u00b7 {length:.1f}s"
        if command.action == "status":
            return self.status_text()
        if command.action == "hardware":
            return f"\U0001f50c {self.hardware_text}"
        if command.action == "whoami":
            return f"Your Telegram user ID is <code>{user_id}</code>."
        return HELP_TEXT

    # -- panel ---------------------------------------------------------------

    def _panel(self, chat_id: int) -> PanelState:
        state = self._panels.get(chat_id)
        if state is None:
            config = self.controller.config
            state = PanelState(config.default_intensity, config.default_auto_off_s)
            self._panels[chat_id] = state
        return state

    def _send_panel(self, chat_id: int) -> None:
        self._send(chat_id, self.panel_text(chat_id), self.panel_keyboard(chat_id))

    def status_text(self) -> str:
        snap = self.controller.snapshot()
        lines = []
        for motor in MOTORS:
            info = snap["motors"][motor]
            if info["active"]:
                detail = f"{_bar(info['intensity'])} {_pct(info['intensity'])} \u00b7 {info['remaining_s']:.0f}s left"
                if snap["independent"] and info["pattern"] != "steady":
                    detail += f" \u00b7 {PATTERN_NAMES[info['pattern']]}"
            else:
                detail = "\u00b7 off"
            lines.append(f"{NAMES[motor]}  {detail}")
        sequence = snap["sequence"]
        if sequence:
            lines.append(f"\u25b6\ufe0f {SEQUENCE_NAMES[sequence['name']]} \u00b7 {sequence['remaining_s']:.1f}s left")
        return "\n".join(lines)

    def panel_text(self, chat_id: int) -> str:
        state = self._panel(chat_id)
        if self.controller.independent:
            mode = "3 independent motors"
        else:
            taps = self.controller.config.taps
            mode = f"1 driver for all motors \u00b7 direction = taps (L {taps['left']} \u00b7 M {taps['middle']} \u00b7 R {taps['right']})"
        settings = f"Strength <b>{_pct(state.intensity)}</b> \u00b7 Auto-off <b>{_duration(state.duration_s)}</b>"
        if self.controller.independent:
            settings += f" \u00b7 Pattern <b>{PATTERN_NAMES[state.pattern]}</b>"
        return "\n".join(
            [
                "<b>\U0001f9af Cane Haptic Remote</b>",
                f"<i>{mode}</i>",
                "",
                self.status_text(),
                "",
                settings,
                "<i>Tap a direction to start it, tap again to stop it.</i>",
            ]
        )

    def panel_keyboard(self, chat_id: int) -> Dict[str, Any]:
        state = self._panel(chat_id)
        snap = self.controller.snapshot()

        def button(text: str, data: str) -> Dict[str, str]:
            return {"text": text, "callback_data": data}

        def motor_button(motor: str) -> Dict[str, str]:
            active = snap["motors"][motor]["active"]
            return button(f"{ON} {BUTTONS[motor]}" if active else BUTTONS[motor], f"m:{motor}")

        all_on = all(snap["motors"][motor]["active"] for motor in MOTORS)
        rows: List[List[Dict[str, str]]] = [
            [motor_button(motor) for motor in MOTORS],
            [button(f"{ON} All" if all_on else "\U0001f500 All", "m:all")]
            + [button(SEQUENCE_NAMES[name], f"seq:{name}") for name in ("sweep", "bounce", "sos")],
            [
                button("\u2796", "i:-"),
                button(f"\U0001f4aa {_pct(state.intensity)}", "noop"),
                button("\u2795", "i:+"),
            ],
            [
                button(("\u2705 " if state.duration_s == value else "") + ("\u221e Hold" if value <= 0 else f"\u23f1 {value:g}s"), f"d:{value:g}")
                for value in DURATION_CHOICES
            ],
        ]
        if self.controller.independent:
            rows.append(
                [
                    button(("\u2705 " if state.pattern == name else "") + PATTERN_NAMES[name], f"p:{name}")
                    for name in PATTERNS
                ]
            )
        rows.append([button("\u23f9 STOP ALL", "stop"), button("\U0001f504", "refresh")])
        return {"inline_keyboard": rows}

    def apply_button(self, chat_id: int, data: str) -> str:
        state = self._panel(chat_id)
        kind, _, value = data.partition(":")
        if kind == "m":
            if value == "all":
                motors = MOTORS
            elif value in MOTORS:
                motors = (value,)
            else:
                return "Unknown button"
            snap = self.controller.snapshot()
            active = [motor for motor in motors if snap["motors"][motor]["active"]]
            if (value == "all" and len(active) == len(MOTORS)) or (value != "all" and active):
                self.controller.stop(motors)
                return "\u23f9 " + ("All" if value == "all" else NAMES[value]) + " off"
            result = self.controller.hold(motors, state.intensity, state.duration_s, state.pattern)
            label = "All" if value == "all" else NAMES[value]
            return f"{label} on \u00b7 {_pct(result.intensity)} \u00b7 {_duration(state.duration_s)}"
        if kind == "seq" and value in SEQUENCE_NAMES:
            length = self.controller.play(value, state.intensity)
            return f"{SEQUENCE_NAMES[value]} \u00b7 {length:.1f}s"
        if kind == "i":
            delta = INTENSITY_STEP if value == "+" else -INTENSITY_STEP
            state.intensity = round(min(1.0, max(0.1, state.intensity + delta)), 2)
            live = self.controller.retune(intensity=state.intensity)
            return f"Strength {_pct(state.intensity)}" + self._live_suffix(live)
        if kind == "d":
            try:
                state.duration_s = float(value)
            except ValueError:
                return "Unknown button"
            return f"Auto-off {_duration(state.duration_s)}"
        if kind == "p" and value in PATTERNS:
            state.pattern = value
            live = self.controller.retune(pattern=value)
            return f"Pattern {PATTERN_NAMES[value]}" + self._live_suffix(live)
        if data == "stop":
            self.controller.stop()
            return "\u23f9 All motors stopped"
        if data == "refresh":
            return "Updated"
        return ""

    def _live_suffix(self, live: tuple) -> str:
        if not live:
            return ""
        return " \u00b7 live on " + " + ".join(NAMES[motor] for motor in live)

    def _on_callback(self, query: Dict[str, Any]) -> None:
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if not self._authorized(user.get("id")):
            LOGGER.warning("refused button from user %s", user.get("id"))
            self._answer(query["id"], "\U0001f512 Not authorized")
            return
        data = str(query.get("data", ""))
        LOGGER.info("user %s (@%s) pressed %s", user.get("id"), user.get("username"), data)
        if chat_id is None:
            self._answer(query["id"], "")
            return
        self._answer(query["id"], self.apply_button(chat_id, data))
        if message_id is not None:
            try:
                self.api.edit_message_text(
                    chat_id, message_id, self.panel_text(chat_id), reply_markup=self.panel_keyboard(chat_id)
                )
            except TelegramError as exc:
                if "not modified" not in exc.description:
                    LOGGER.warning("panel refresh failed: %s", exc)
            except OSError as exc:
                LOGGER.warning("panel refresh failed: %s", exc)

    def _answer(self, query_id: str, text: str) -> None:
        try:
            self.api.answer_callback_query(query_id, text or None)
        except (TelegramError, OSError) as exc:
            LOGGER.warning("answerCallbackQuery failed: %s", exc)
