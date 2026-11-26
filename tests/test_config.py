import json
from pathlib import Path

import pytest

from ane_haptic_remote.config import (
    MOTORS,
    BotSettings,
    ConfigError,
    HardwareConfig,
    load_dotenv,
    load_hardware_config,
    parse_user_ids,
)

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_are_a_valid_single_driver_configuration():
    config = HardwareConfig()
    assert config.topology == "auto"
    assert set(config.channels) == set(MOTORS)
    assert config.taps == {"left": 1, "middle": 2, "right": 3}


@pytest.mark.parametrize("name", ["three-drv2605l.json", "single-drv2605l.json"])
def test_shipped_configs_load(name):
    config = load_hardware_config(ROOT / "configs" / name)
    assert config.drv_address == 0x5A
    assert config.mux_address == 0x70
    assert config.motor_voltage_v == 3.0


def test_three_driver_config_maps_each_motor_to_a_mux_channel():
    config = load_hardware_config(ROOT / "configs" / "three-drv2605l.json")
    assert config.channels == {"left": 4, "middle": 5, "right": 6}
    assert config.single_mux_channel is None


def test_single_driver_config_uses_tap_codes():
    config = load_hardware_config(ROOT / "configs" / "single-drv2605l.json")
    assert config.topology == "single_drv"
    assert config.single_mux_channel is None  # scan every mux channel for the one chip
    assert config.taps == {"left": 1, "middle": 2, "right": 3}


def test_addresses_accept_strings(tmp_path):
    path = tmp_path / "hw.json"
    path.write_text(json.dumps({"mux_address": "0x71", "drv_address": "0x5b"}))
    config = load_hardware_config(path)
    assert (config.mux_address, config.drv_address) == (0x71, 0x5B)


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "hw.json"
    path.write_text(json.dumps({"topology": "simulate", "from_the_future": True}))
    assert load_hardware_config(path).topology == "simulate"


@pytest.mark.parametrize(
    "payload",
    [
        {"channels": {"left": 4, "middle": 5}},
        {"channels": {"left": 4, "middle": 4, "right": 6}},
        {"channels": {"left": 4, "middle": 5, "right": 9}},
        {"taps": {"left": 0, "middle": 2, "right": 3}},
        {"topology": "magic"},
        {"amplitude_floor": 1.0},
        {"default_auto_off_s": -1},
        {"tap_on_ms": 0},
    ],
)
def test_invalid_configuration_is_rejected(payload):
    with pytest.raises(ConfigError):
        HardwareConfig.from_dict(payload)


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        load_hardware_config(tmp_path / "nope.json")


def test_broken_json_is_reported(tmp_path):
    path = tmp_path / "hw.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load_hardware_config(path)


def test_parse_user_ids():
    assert parse_user_ids("123, 456;789") == frozenset({123, 456, 789})
    assert parse_user_ids("   ") == frozenset()
    with pytest.raises(ConfigError):
        parse_user_ids("123,chip")


def test_settings_require_a_real_token():
    with pytest.raises(ConfigError):
        BotSettings.from_env({"TELEGRAM_BOT_TOKEN": "paste_your_bot_token_here"})
    with pytest.raises(ConfigError):
        BotSettings.from_env({})
    assert BotSettings.from_env({}, require_token=False).token == ""


def test_settings_read_the_environment():
    settings = BotSettings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "ALLOWED_USER_IDS": "42",
            "HAPTIC_TOPOLOGY": "three_drv",
            "HAPTIC_CONFIG": "/etc/hw.json",
            "DEFAULT_AUTO_OFF_S": "4.5",
        }
    )
    assert settings.token == "123:abc"
    assert settings.allowed_user_ids == frozenset({42})
    assert settings.topology == "three_drv"
    assert settings.config_path == Path("/etc/hw.json")
    assert settings.default_auto_off_s == 4.5


def test_settings_reject_a_bad_topology():
    with pytest.raises(ConfigError):
        BotSettings.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "HAPTIC_TOPOLOGY": "nope"})


def test_settings_reject_a_bad_auto_off():
    with pytest.raises(ConfigError):
        BotSettings.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "DEFAULT_AUTO_OFF_S": "soon"})


def test_load_dotenv_does_not_override_the_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN=from_file\nALLOWED_USER_IDS="42, 43"\n# comment\n\n')
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    assert load_dotenv(env_file) is True
    assert load_dotenv(tmp_path / "missing") is False
    settings = BotSettings.from_env()
    assert settings.token == "from_file"
    assert settings.allowed_user_ids == frozenset({42, 43})
