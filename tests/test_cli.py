import pytest

from ane_haptic_remote import cli
from ane_haptic_remote.config import BotSettings


def test_main_without_a_subcommand_exits(capsys):
    with pytest.raises(SystemExit):
        cli.main([])


def test_run_without_a_token_fails_cleanly(capsys):
    code = cli.main(["--env", "does-not-exist", "run"])
    assert code == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_test_command_plays_in_simulation(capsys, monkeypatch):
    monkeypatch.setenv("HAPTIC_TOPOLOGY", "simulate")
    code = cli.main(["--env", "does-not-exist", "test", "left", "80", "200ms"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Simulated" in out
    assert "L #" in out


def test_test_command_rejects_a_non_playable_request(capsys):
    code = cli.main(["--env", "does-not-exist", "--topology", "simulate", "test", "stop"])
    assert code == 2


def test_scan_fails_cleanly_without_i2c(monkeypatch, capsys):
    def boom(_bus):
        raise RuntimeError("no /dev/i2c-1 on this machine")

    monkeypatch.setattr(cli, "open_smbus", boom)
    assert cli.main(["--env", "does-not-exist", "scan"]) == 1
    assert "no /dev/i2c-1" in capsys.readouterr().err


def test_auto_off_from_the_environment_overrides_the_config(monkeypatch):
    monkeypatch.setenv("DEFAULT_AUTO_OFF_S", "2.5")
    settings = BotSettings.from_env({"TELEGRAM_BOT_TOKEN": "1:a", "DEFAULT_AUTO_OFF_S": "2.5"})
    assert settings.default_auto_off_s == 2.5
