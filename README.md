# ane-haptic-remote

A Telegram remote control for the Smart Cane's haptic motors. One bot on the Raspberry Pi 5, one chat window on your phone: a live inline panel with **Left / Middle / Right**, strength, duration and pattern, plus plain-text commands you can type mid-session.

It speaks to the same DRV2605L haptics already wired in [`capstone-pi-benchmarking`](https://github.com/ctxnn/capstone-pi-benchmarking), and it adapts itself to whichever build it finds:

| Wiring | Hardware | How direction works |
| --- | --- | --- |
| `three_drv` | Three DRV2605L, one per motor, behind the TCA9548A at `0x70` (mux channels 4/5/6) | Each motor vibrates on its own — a left cue is the left ERM alone |
| `single_drv` | One DRV2605L at `0x5A` driving all three ERMs in parallel (the current harness) | Direction is a tap code: left = 1 tap, middle = 2 taps, right = 3 taps |

The topology is detected at startup (`ane-remote scan` shows what it sees). The bot never guesses at hardware: if nothing answers, it says so and refuses to run.

## Safety, because this buzzes a cane someone is holding

- Every command has an **end time**. The bot outlives your network, not the other way around: a default 10 s auto-off, and a hard cap of 60 s for any hold, so a lost phone cannot leave a motor running.
- `stop` works from the panel, from text, and takes effect immediately.
- The controller thread parks the motors **on every exit path** — Ctrl-C, SIGTERM, systemd restart, an exception in the loop.
- Only the Telegram user IDs you list can do anything. An unknown sender gets their own ID back and instructions, and the motors stay quiet.
- I2C failures are counted, logged, and retried; three in a row re-initialises the driver instead of dying silently.

## Install on the Pi

```bash
git clone https://github.com/ctxnn/ane-haptic-remote.git
cd ane-haptic-remote
./deploy/install-on-pi.sh        # enables I2C, installs .[pi], copies .env.example
```

Or by hand:

```bash
sudo raspi-config nonint do_i2c 0        # enable I2C
python3 -m venv .venv
.venv/bin/pip install -e ".[pi]"
cp .env.example .env
```

### Get a bot token

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, pick a name.
2. Put the token in `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...your-token...
ALLOWED_USER_IDS=
HAPTIC_TOPOLOGY=auto
HAPTIC_CONFIG=
DEFAULT_AUTO_OFF_S=10
```

### Lock it to yourself

```bash
.venv/bin/ane-remote run
```

Open the bot, send `/whoami`. It replies with your numeric user ID. Put it in `.env` (`ALLOWED_USER_IDS=123456789`, comma-separated for more than one person), restart the bot. Until then the remote is locked to everyone, including you.

### Check the wiring before trusting it

```bash
.venv/bin/ane-remote scan
```

Prints every address on the bus and behind the mux, then the detected topology:

```
    main: 0x70
    ch4: 0x5a
    ch5: 0x5a
    ch6: 0x5a

detected: 3x DRV2605L, independent motors, behind TCA9548A 0x70: left CH4, middle CH5, right CH6
```

If the cane service is running, stop it first — it drives the same chip:

```bash
sudo systemctl stop yolo-pi-cane
```

### Run it

```bash
.venv/bin/ane-remote run
```

Then in Telegram: `/remote` for the panel, or just type a command.

## Using it

The panel is a single message that edits itself, so the chat stays clean.

```
⏺️ Cane Haptic Remote
3 independent motors

⬅️ Left  · off
⏺ Middle  ▪▪▪▪▫ 80% · 7s left
➡️ Right · off

Strength 80% · Auto-off 10s · Pattern Steady
```

Buttons: the three directions (tap to start, tap again to stop), **All**, `↔️ Sweep`, `🔁 Bounce`, `🆘 SOS`, `➖ / 💪 / ➕` for strength, `3s / 10s / 30s / ∞`, the four patterns, and **STOP ALL**.

Text commands — the same engine, no button needed:

| You type | It does |
| --- | --- |
| `left` · `middle` · `right` | Buzz that motor at the panel's strength |
| `all` · `left and right` | Several motors at once |
| `right 60%` · `left 40` | Set strength for this one command |
| `left 5s` · `middle max 500ms` | Set duration (s, ms, min) |
| `right 80 5s` | Strength and duration together |
| `middle hold` | Until `stop` (still capped at 60 s) |
| `left pulse` · `all heartbeat` · `right ramp` | Patterns on independent motors |
| `sweep` · `bounce` · `sos` · `test` | Choreographed sequences |
| `stop` · `stop left` | Stop everything, or one motor |
| `status` | What is buzzing right now, and for how much longer |
| `hardware` | Detected wiring |
| `whoami` | Your Telegram user ID |
| `help` | The cheat sheet |

**Left / middle / right** map to the cane's three ERMs. On single-driver hardware the bot says so in the panel header and plays the tap code instead: a left cue is one short buzz, middle is two, right is three.

## Run as a service

```bash
sudo cp deploy/ane-haptic-remote.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ane-haptic-remote
journalctl -u ane-haptic-remote -f
```

The unit stops `yolo-pi-cane` conflicts by asking you to stop that service yourself; it does not touch it. Run one or the other, or point them at different chips.

## Test on a laptop, no hardware

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest

HAPTIC_TOPOLOGY=simulate .venv/bin/ane-remote test left 80 2s   # prints a live level bar
HAPTIC_TOPOLOGY=simulate .venv/bin/ane-remote test sweep
```

`simulate` pretends it has independent motors; `simulate_single` pretends it has one driver and plays the tap code. Both exercise the real controller, the real command parser and the real bot code paths.

The suite covers the parts you cannot see on a bench: that `left` reaches only the left driver's RTP register on a three-driver build, that `stop` and the auto-off timer zero every motor, that a stranger's button press moves nothing, and that a hold never outlives its cap.

## Layout

```
src/ane_haptic_remote/
  config.py        settings from .env plus the hardware JSON
  patterns.py      patterns, sequences, tap codes, timeline evaluation (no hardware)
  hardware.py      I2C, TCA9548A mux, DRV2605L open-loop ERM driver, wiring detection, simulated backends
  controller.py    holds, auto-off, sequences; the 200 Hz render loop that owns the motors
  commands.py      what people type -> a command
  telegram_api.py  the Bot API over urllib (long polling, no extra dependency)
  bot.py           panel, buttons, text dispatch, allow-list
  cli.py           ane-remote run | scan | test
configs/
  three-drv2605l.json    three chips, one per motor
  single-drv2605l.json   one chip, tap-code direction
deploy/
  install-on-pi.sh
  ane-haptic-remote.service
```

## Tuning

`configs/*.json` is the place, and every value is validated on load:

| Key | Meaning |
| --- | --- |
| `amplitude_floor` | Where an ERM actually starts spinning (default `0.30`); below it you get silence, not a light buzz |
| `motor_voltage_v` | Sets `OD_CLAMP` (`3.0` V for the ERMs in the cane) |
| `taps` | Tap counts per direction on single-driver hardware |
| `tap_on_ms` / `tap_off_ms` / `sector_gap_ms` / `group_gap_ms` | Tap rhythm |
| `default_auto_off_s` / `max_continuous_on_s` | Safety timers |
| `channels` / `single_mux_channel` | Where each chip sits |

Point the bot at a different file with `HAPTIC_CONFIG=/etc/cane-haptic.json`.

## Limits worth knowing

- Telegram long polling only — no webhook, no inbound port, nothing to expose on your LAN.
- One panel per chat; the strength/duration/pattern you set in a chat applies to that chat.
- The bot does not read the cane's sensors. It is a remote control for the motors, not a second copy of the cue logic.
- `single_drv` direction is a code you learn, not a spatial cue: with one driver and three parallel ERMs, all three buzz together by definition.
