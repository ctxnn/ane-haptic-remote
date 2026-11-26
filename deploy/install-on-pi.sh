#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Enabling I2C on Raspberry Pi (if raspi-config is present)"
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true
fi

echo "==> Creating virtual environment and installing ane-haptic-remote[pi]"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv
  uv pip install --python .venv/bin/python -e ".[pi]"
else
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e ".[pi]"
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env from .env.example — edit .env to add your TELEGRAM_BOT_TOKEN"
fi

echo "==> Scanning I2C bus for TCA9548A (0x70) and DRV2605L (0x5A)"
.venv/bin/ane-remote scan || true

echo ""
echo "Done! Next steps:"
echo "  1. Edit $REPO_DIR/.env and set TELEGRAM_BOT_TOKEN"
echo "  2. Run: $REPO_DIR/.venv/bin/ane-remote run"
echo "  3. Optional auto-start on boot:"
echo "     sudo cp $REPO_DIR/deploy/ane-haptic-remote.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload && sudo systemctl enable --now ane-haptic-remote"
