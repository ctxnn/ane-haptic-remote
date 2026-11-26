import pytest

from ane_haptic_remote.commands import parse_command


def test_empty_input_is_ignored():
    assert parse_command(None) is None
    assert parse_command("   ") is None


def test_plain_direction():
    command = parse_command("left")
    assert command.action == "hold"
    assert command.motors == ("left",)
    assert command.intensity is None and command.duration_s is None


@pytest.mark.parametrize("word", ["right", "RIGHT", " Right ", "rt", "/right", "arrow"])
def test_direction_synonyms(word):
    command = parse_command("right" if word == "arrow" else word)
    assert command is not None and command.motors == ("right",)


def test_bot_suffix_is_stripped():
    assert parse_command("/left@cane_remote_bot").motors == ("left",)


def test_center_means_middle():
    assert parse_command("centre").motors == ("middle",)
    assert parse_command("center").motors == ("middle",)


def test_direction_with_percent():
    command = parse_command("right 60%")
    assert command.motors == ("right",)
    assert command.intensity == pytest.approx(0.6)


def test_direction_with_duration_in_seconds():
    command = parse_command("left 5s")
    assert command.motors == ("left",)
    assert command.duration_s == 5.0
    assert command.intensity is None


def test_direction_with_milliseconds():
    assert parse_command("middle max 500ms").duration_s == pytest.approx(0.5)


def test_direction_with_minutes():
    assert parse_command("left 2 minutes").duration_s == pytest.approx(120.0)


def test_intensity_and_duration_together():
    command = parse_command("right 80 5s")
    assert (command.motors, command.intensity, command.duration_s) == (("right",), pytest.approx(0.8), 5.0)


def test_fractional_intensity_below_one_is_taken_as_fraction():
    assert parse_command("left 0.5").intensity == pytest.approx(0.5)


def test_percent_word():
    assert parse_command("left 40 percent").intensity == pytest.approx(0.4)


def test_soft_and_strong():
    assert parse_command("left soft").intensity == pytest.approx(0.4)
    assert parse_command("left max").intensity == pytest.approx(1.0)


def test_two_motors_with_a_connector():
    assert parse_command("left and right soft").motors == ("left", "right")


def test_all_motors():
    assert parse_command("all").motors == ("left", "middle", "right")
    assert parse_command("everything 50%").intensity == pytest.approx(0.5)


def test_hold_means_until_stopped():
    command = parse_command("middle hold")
    assert command.duration_s == 0.0


def test_patterns():
    assert parse_command("left pulse").pattern == "pulse"
    assert parse_command("right heartbeat").pattern == "heartbeat"
    assert parse_command("all ramp").pattern == "ramp"
    assert parse_command("left steady").pattern == "steady"


def test_stop():
    assert parse_command("stop").action == "stop"
    assert parse_command("STOP left").action == "stop"
    assert parse_command("stop left").motors == ("left",)


def test_sequences():
    for word, name in (("sweep", "sweep"), ("bounce", "bounce"), ("sos", "sos"), ("test", "test")):
        command = parse_command(word)
        assert command.action == "sequence" and command.sequence == name


def test_sequence_takes_intensity():
    command = parse_command("sos 40%")
    assert command.sequence == "sos" and command.intensity == pytest.approx(0.4)


def test_specials():
    assert parse_command("status").action == "status"
    assert parse_command("help").action == "help"
    assert parse_command("remote").action == "panel"
    assert parse_command("whoami").action == "whoami"
    assert parse_command("hardware").action == "hardware"


def test_intensity_is_clamped():
    assert parse_command("left 500%").intensity == 1.0


def test_emoji_arrows_work():
    assert parse_command("\u2b05\ufe0f").motors == ("left",)
    assert parse_command("\u27a1\ufe0f").motors == ("right",)


def test_gibberish_is_ignored():
    assert parse_command("hello there") is None
    assert parse_command("80") is None
