import pytest
from conftest import FakeTelegram

from ane_haptic_remote.bot import RemoteBot
from ane_haptic_remote.commands import parse_command
from ane_haptic_remote.config import MOTORS, BotSettings, HardwareConfig
from ane_haptic_remote.controller import HapticController
from ane_haptic_remote.hardware import SimulatedBackend

OWNER = 4242
CHAT = 4242


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_bot(independent=True, allowed=(OWNER,)):
    clock = Clock()
    backend = SimulatedBackend(independent=independent)
    controller = HapticController(backend, HardwareConfig(), clock=clock)
    api = FakeTelegram()
    settings = BotSettings(token="123:abc", allowed_user_ids=frozenset(allowed))
    bot = RemoteBot(api, controller, settings, "simulated motors")
    return bot, api, controller, backend, clock


def message(text, user=OWNER, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": user, "username": "cane"}, "text": text}}


def press(data, user=OWNER, message_id=7):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb",
            "from": {"id": user, "username": "cane"},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": CHAT}},
        },
    }


def settle(bot, clock, seconds=0.0):
    clock.advance(seconds)
    bot.controller.backend.write(bot.controller.render(clock()))
    return bot.controller.backend.levels


@pytest.mark.parametrize("word,motor", [("left", "left"), ("middle", "middle"), ("right", "right")])
def test_typing_a_direction_buzzes_only_that_motor(word, motor):
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(message(word))
    levels = settle(bot, clock)
    assert levels[motor] > 0
    assert all(levels[other] == 0 for other in MOTORS if other != motor)
    assert motor.title() in api.last_text or motor in api.last_text


def test_typing_right_80_5s_sets_both():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(message("right 80 5s"))
    levels = settle(bot, clock)
    assert levels["right"] == pytest.approx(0.8)
    assert controller.snapshot(clock())["motors"]["right"]["remaining_s"] == pytest.approx(5.0)


def test_stop_zeroes_everything():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(message("all"))
    settle(bot, clock)
    bot.handle_update(message("stop"))
    levels = settle(bot, clock)
    assert all(levels[motor] == 0.0 for motor in MOTORS)
    assert "stopped" in api.last_text.lower() or "off" in api.last_text.lower()


def test_auto_off_expires_from_telegram():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(message("left"))
    settle(bot, clock)
    settle(bot, clock, controller.config.default_auto_off_s + 0.1)
    assert all(level == 0.0 for level in backend.levels.values())


def test_unauthorized_user_is_refused_and_motors_stay_quiet():
    bot, api, controller, backend, clock = make_bot(allowed=(OWNER,))
    bot.handle_update(message("left", user=99))
    levels = settle(bot, clock)
    assert all(level == 0.0 for level in levels.values())
    assert "locked" in api.last_text.lower()


def test_an_empty_allow_list_locks_everyone_out():
    bot, api, *_ = make_bot(allowed=())
    bot.handle_update(message("left"))
    assert "locked" in api.last_text.lower()


def test_whoami_works_without_authorization():
    bot, api, *_ = make_bot(allowed=())
    bot.handle_update(message("/whoami", user=7))
    assert "7" in api.last_text


def test_help_and_gibberish():
    bot, api, *_ = make_bot()
    bot.handle_update(message("help"))
    assert "Cane Haptic Remote" in api.last_text
    bot.handle_update(message("banana"))
    assert "didn't catch that" in api.last_text


def test_sequence_command():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(message("sweep"))
    assert controller.snapshot(clock())["sequence"]["name"] == "sweep"
    settle(bot, clock)
    assert any(level > 0 for level in backend.levels.values())


def test_panel_has_a_button_for_every_motor():
    bot, api, *_ = make_bot()
    bot.handle_update(message("/remote"))
    markup = api.messages[-1]["markup"]["inline_keyboard"]
    data = [button["callback_data"] for row in markup for button in row]
    assert {"m:left", "m:middle", "m:right", "m:all", "stop"} <= set(data)
    assert any(item.startswith("seq:") for item in data)
    assert any(item.startswith("i:") for item in data)
    assert any(item.startswith("d:") for item in data)
    assert "Left" in api.messages[-1]["text"] and "Right" in api.messages[-1]["text"]


def test_panel_button_press_runs_and_refreshes():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(message("/remote"))
    bot.handle_update(press("m:right"))
    levels = settle(bot, clock)
    assert levels["right"] > 0 and levels["left"] == 0.0
    assert api.answers[-1]["text"]
    panel = api.panels[(CHAT, 7)]
    assert "Right" in panel["text"]


def test_pressing_an_active_motor_stops_it():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(press("m:left"))
    settle(bot, clock)
    bot.handle_update(press("m:left"))
    levels = settle(bot, clock)
    assert levels["left"] == 0.0


def test_all_button_toggles():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(press("m:all"))
    levels = settle(bot, clock)
    assert all(levels[motor] > 0 for motor in MOTORS)
    bot.handle_update(press("m:all"))
    levels = settle(bot, clock)
    assert all(levels[motor] == 0.0 for motor in MOTORS)


def test_stop_button():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(press("m:all"))
    bot.handle_update(press("stop"))
    levels = settle(bot, clock)
    assert all(level == 0.0 for level in levels.values())


def test_intensity_buttons_change_the_next_hold():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(press("i:-"))
    bot.handle_update(press("i:-"))
    bot.handle_update(press("m:left"))
    levels = settle(bot, clock)
    assert levels["left"] == pytest.approx(0.6)


def test_changing_strength_retunes_a_motor_that_is_already_running():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(press("m:left"))
    bot.handle_update(press("i:-"))
    levels = settle(bot, clock)
    assert levels["left"] == pytest.approx(0.7)
    assert "live on" in api.answers[-1]["text"]
    remaining = controller.snapshot(clock())["motors"]["left"]["remaining_s"]
    assert remaining > 9.0


def test_changing_the_pattern_retunes_a_motor_that_is_already_running():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(press("m:left"))
    bot.handle_update(press("p:pulse"))
    assert controller.render(clock())["left"] > 0
    assert controller.render(clock() + 0.3)["left"] == 0.0


def test_intensity_is_clamped():
    bot, _, _, _, _ = make_bot()
    panel = bot._panel(CHAT)
    for _ in range(20):
        bot.apply_button(CHAT, "i:")
    assert panel.intensity == pytest.approx(0.1)
    for _ in range(20):
        bot.apply_button(CHAT, "i:+")
    assert panel.intensity == pytest.approx(1.0)


def test_duration_buttons():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(press("d:30"))
    bot.handle_update(press("m:middle"))
    assert controller.snapshot(clock())["motors"]["middle"]["remaining_s"] == pytest.approx(30.0)


def test_hold_button_uses_the_cap():
    bot, _, controller, _, clock = make_bot()
    bot.handle_update(press("d:0"))
    bot.handle_update(press("m:middle"))
    snapshot = controller.snapshot(clock())
    assert snapshot["motors"]["middle"]["remaining_s"] == pytest.approx(controller.config.max_continuous_on_s)


def test_pattern_buttons_affect_independent_motors():
    bot, _, controller, backend, clock = make_bot()
    bot.handle_update(press("p:pulse"))
    bot.handle_update(press("m:left"))
    on = controller.render(clock())["left"]
    off = controller.render(clock() + 0.3)["left"]
    assert on > 0 and off == 0.0


def test_shared_hardware_panel_explains_the_tap_code():
    bot, api, *_ = make_bot(independent=False)
    bot.handle_update(message("/remote"))
    text = api.messages[-1]["text"]
    assert "tap" in text.lower()
    markup = api.messages[-1]["markup"]["inline_keyboard"]
    data = [button["callback_data"] for row in markup for button in row]
    assert not any(item.startswith("p:") for item in data)


def test_shared_hardware_still_toggles_every_motor():
    bot, _, controller, backend, clock = make_bot(independent=False)
    for word, motor in (("left", "left"), ("middle", "middle"), ("right", "right")):
        bot.handle_update(message(word))
        assert controller.snapshot(clock())["motors"][motor]["active"] is True
    bot.handle_update(message("stop"))
    assert all(not info["active"] for info in controller.snapshot(clock())["motors"].values())


def test_buttons_from_a_stranger_are_refused():
    bot, api, controller, backend, clock = make_bot(allowed=(OWNER,))
    bot.handle_update(press("m:all", user=555))
    levels = settle(bot, clock)
    assert all(level == 0.0 for level in levels.values())
    assert "Not authorized" in api.answers[-1]["text"]


def test_status_reports_live_state():
    bot, api, controller, backend, clock = make_bot()
    bot.handle_update(message("left 90 4s"))
    bot.handle_update(message("status"))
    assert "4s left" in api.last_text or "3s left" in api.last_text


def test_offline_commands_are_skipped_on_start():
    bot, api, *_ = make_bot()
    api.updates = [message("stop")]
    offset = bot._skip_pending()
    assert offset == 2
    assert api.updates == []


def test_run_forever_handles_one_update_then_stops():
    bot, api, controller, backend, clock = make_bot()
    api.updates = [message("right")]
    import threading

    stop = threading.Event()

    calls = {"n": 0}

    def one_shot(_self, offset=None, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # the startup skip sees nothing queued
        pending = FakeTelegram.get_updates(_self, offset, timeout)
        stop.set()
        return pending

    api.get_updates = one_shot.__get__(api, type(api))
    bot.run_forever(stop)
    assert calls["n"] >= 2
    assert controller.snapshot(clock())["motors"]["right"]["active"] is True
    assert api.commands and api.commands[0][0] == "remote"


def test_execute_accepts_parsed_commands():
    bot, _, controller, backend, clock = make_bot()
    text = bot.execute(parse_command("left 50 2s"), CHAT, OWNER)
    assert "Left" in text and "50%" in text
    assert settle(bot, clock)["left"] == pytest.approx(0.5)
