import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FakeI2CBus:
    """A tiny TCA9548A + DRV2605L simulator: only wired addresses answer, like real I2C."""

    MUX = 0x70

    def __init__(self, main=(), channels=None, device_id=4):
        self.main = set(main)
        self.channels = {channel: set(addresses) for channel, addresses in (channels or {}).items()}
        self.selected = None
        self.registers = {}
        self.writes = []
        self.closed = False
        self.device_id = device_id
        for address in self.main:
            self._seed(None, address)
        for channel, addresses in self.channels.items():
            for address in addresses:
                self._seed(channel, address)

    def _seed(self, path, address):
        registers = self.registers.setdefault((path, address), {})
        registers[0x00] = self.device_id << 5
        registers.setdefault(0x1A, 0x00)
        registers.setdefault(0x1D, 0x00)

    def _visible(self):
        return self.main if self.selected is None else self.channels.get(self.selected, set())

    def _check(self, address):
        visible = set(self._visible())
        if address == self.MUX and self.MUX in self.main:
            visible.add(self.MUX)
        if address not in visible:
            raise OSError(121, "Remote I/O error")

    def read_byte(self, address):
        self._check(address)
        return 0

    def read_byte_data(self, address, register):
        self._check(address)
        return self.registers[(self.selected, address)].get(register, 0)

    def write_byte(self, address, value):
        self._check(address)
        if address == self.MUX:
            channels = [channel for channel in range(8) if value & (1 << channel)]
            self.selected = channels[0] if channels else None
            self.writes.append(("mux", self.selected))
            return
        self.writes.append(("byte", address, value))

    def write_byte_data(self, address, register, value):
        self._check(address)
        self.registers[(self.selected, address)][register] = value & 0xFF
        self.writes.append(("reg", self.selected, address, register, value & 0xFF))

    def close(self):
        self.closed = True

    def register(self, channel, address, register):
        return self.registers.get((channel, address), {}).get(register)

    def path_names(self):
        return sorted(name for name, _ in self.registers)


class FakeTelegram:
    """Records Bot API calls and hands back queued updates."""

    def __init__(self):
        self.messages = []
        self.panels = {}
        self.answers = []
        self.commands = None
        self.updates = []

    def set_my_commands(self, commands):
        self.commands = list(commands)

    def get_me(self):
        return {"id": 1, "username": "cane_remote_bot"}

    def get_updates(self, offset=None, timeout=0):
        pending = [update for update in self.updates if offset is None or update["update_id"] >= offset]
        self.updates = []
        return pending

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.panels[(chat_id, message_id)] = {"text": text, "markup": reply_markup}
        return True

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append({"id": callback_query_id, "text": text})
        return True

    @property
    def last_text(self):
        return self.messages[-1]["text"] if self.messages else None


@pytest.fixture
def fake_bus():
    return FakeI2CBus


@pytest.fixture
def telegram():
    return FakeTelegram()
