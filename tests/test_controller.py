import pytest

from ane_haptic_remote.config import MOTORS, HardwareConfig
from ane_haptic_remote.controller import HapticController
from ane_haptic_remote.hardware import SimulatedBackend


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def build(config=None, independent=True):
    clock = Clock()
    backend = SimulatedBackend(independent=independent)
    controller = HapticController(backend, config or HardwareConfig(), clock=clock)
    return controller, backend, clock


@pytest.mark.parametrize("motor", MOTORS)
def test_a_single_hold_drives_only_that_motor(motor):
    controller, backend, clock = build()
    controller.hold([motor], intensity=0.8, duration_s=5.0)
    backend.write(controller.render(clock()))
    levels = backend.levels
    assert levels[motor] == pytest.approx(0.8)
    assert all(levels[other] == 0.0 for other in MOTORS if other != motor)


def test_hold_defaults_come_from_the_config():
    controller, _, _ = build()
    result = controller.hold(["left"])
    config = controller.config
    assert result.intensity == pytest.approx(config.default_intensity)
    assert result.duration_s == pytest.approx(config.default_auto_off_s)


def test_hold_is_capped_at_the_maximum_continuous_time():
    controller, _, _ = build()
    result = controller.hold(["left"], duration_s=10_000.0)
    assert result.duration_s == controller.config.max_continuous_on_s
    assert result.until_stopped is False


def test_hold_forever_still_has_a_hard_cap():
    controller, backend, clock = build()
    result = controller.hold(["left"], duration_s=0.0)
    assert result.until_stopped is True
    cap = controller.config.max_continuous_on_s
    assert controller.snapshot(clock() + cap - 0.1)["motors"]["left"]["active"] is True
    assert controller.snapshot(clock() + cap + 0.1)["motors"]["left"]["active"] is False


def test_auto_off_clears_the_motor():
    controller, _, clock = build()
    controller.hold(["right"], intensity=0.9, duration_s=2.0)
    assert controller.render(clock())["right"] > 0
    clock.advance(1.9)
    assert controller.render(clock())["right"] > 0
    clock.advance(0.2)
    assert controller.render(clock())["right"] == 0.0


def test_stop_clears_everything():
    controller, _, clock = build()
    controller.hold(["left", "right"], intensity=0.9, duration_s=5.0)
    assert controller.render(clock())["left"] > 0
    controller.stop()
    levels = controller.render(clock())
    assert all(levels[motor] == 0.0 for motor in MOTORS)


def test_stop_of_one_motor_leaves_the_others_running():
    controller, _, clock = build()
    controller.hold(["left"], intensity=0.9, duration_s=5.0)
    controller.hold(["right"], intensity=0.9, duration_s=5.0)
    controller.stop(["left"])
    levels = controller.render(clock())
    assert levels["left"] == 0.0 and levels["right"] > 0


def test_zero_intensity_is_treated_as_off():
    controller, backend, clock = build()
    result = controller.hold(["left"], intensity=0.0, duration_s=1.0)
    assert result.intensity == 0.0
    backend.write(controller.render(clock()))
    assert backend.levels["left"] == 0.0
    assert controller.snapshot(clock())["motors"]["left"]["active"] is False


def test_hold_rejects_an_empty_selection():
    controller, _, _ = build()
    with pytest.raises(ValueError):
        controller.hold([])


def test_hold_rejects_an_unknown_pattern():
    controller, _, _ = build()
    with pytest.raises(ValueError):
        controller.hold(["left"], pattern="disco")


def test_sequence_plays_independently():
    controller, _, clock = build()
    length = controller.play("test", 0.7)
    assert length > 0
    levels = [controller.render(clock() + offset) for offset in (0.0, 1.2, 2.4)]
    assert levels[0]["left"] > 0
    assert levels[1]["middle"] > 0
    assert levels[2]["right"] > 0
    assert controller.render(clock() + length + 0.1) == {motor: 0.0 for motor in MOTORS}


def test_a_hold_cancels_a_running_sequence():
    controller, _, clock = build()
    controller.play("sweep", 0.9)
    controller.hold(["right"], intensity=0.9, duration_s=3.0)
    assert controller.snapshot(clock())["sequence"] is None


def test_a_sequence_cancels_a_running_hold():
    controller, _, clock = build()
    controller.hold(["right"], intensity=0.9, duration_s=3.0)
    controller.play("sos", 0.9)
    assert not controller.snapshot(clock())["motors"]["right"]["active"]


def test_shared_driver_taps_one_motor_at_a_time():
    controller, backend, clock = build(independent=False)
    assert controller.independent is False
    controller.hold(["left"], intensity=0.8, duration_s=5.0)
    levels = [controller.render(clock() + offset)[ "left"] for offset in (0.0, 0.1, 0.2, 0.3)]
    assert levels[0] > 0 and any(level == 0 for level in levels)
    backend.write(controller.render(clock()))
    assert backend.levels["left"] > 0


def test_shared_driver_cycles_between_two_active_motors():
    controller, _, clock = build(independent=False)
    controller.hold(["left"], intensity=0.8, duration_s=5.0)
    controller.hold(["right"], intensity=0.8, duration_s=5.0)
    seen = set()
    for step in range(200):
        levels = controller.render(clock() + step * 0.01)
        for motor in MOTORS:
            if levels[motor] > 0:
                seen.add(motor)
    assert "left" in seen and "right" in seen


def test_shared_driver_sequence_is_encoded_as_taps():
    controller, _, clock = build(independent=False)
    length = controller.play("sweep", 0.8, repeat=1)
    assert length > 0
    assert controller.render(clock())["left"] > 0


def test_retune_changes_strength_without_moving_the_end_time():
    controller, backend, clock = build()
    controller.hold(["left"], intensity=0.9, duration_s=5.0)
    before = controller.snapshot(clock() + 1.0)["motors"]["left"]["remaining_s"]
    assert controller.retune(intensity=0.4) == ("left",)
    after = controller.snapshot(clock() + 1.0)["motors"]["left"]
    assert after["remaining_s"] == pytest.approx(before)
    backend.write(controller.render(clock() + 1.0))
    assert backend.levels["left"] == pytest.approx(0.4)


def test_retune_changes_the_pattern_in_place():
    controller, _, clock = build()
    controller.hold(["right"], intensity=0.9, duration_s=5.0)
    controller.retune(pattern="pulse")
    assert controller.render(clock())["right"] > 0
    assert controller.render(clock() + 0.3)["right"] == 0.0


def test_retune_with_nothing_running_is_a_no_op():
    controller, _, _ = build()
    assert controller.retune(intensity=0.5) == ()
    with pytest.raises(ValueError):
        controller.retune(pattern="disco")


def test_snapshot_reports_remaining_time():
    controller, _, clock = build()
    controller.hold(["middle"], intensity=0.8, duration_s=4.0)
    snapshot = controller.snapshot(clock() + 1.0)
    assert snapshot["motors"]["middle"]["active"] is True
    assert snapshot["motors"]["middle"]["remaining_s"] == pytest.approx(3.0)
    assert snapshot["motors"]["left"]["active"] is False


def test_close_parks_the_motors():
    controller, backend, clock = build()
    controller.hold(["left", "middle", "right"], intensity=1.0, duration_s=9.0)
    backend.write(controller.render(clock()))
    assert backend.levels["left"] > 0
    controller.close()
    assert all(backend.levels[motor] == 0.0 for motor in MOTORS)
    assert backend.closed is True


def test_the_thread_writes_to_the_backend():
    import time

    controller, backend, _ = build()
    controller.start()
    try:
        controller.hold(["right"], intensity=0.9, duration_s=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not backend.changes:
            time.sleep(0.005)
        assert backend.changes
        assert backend.levels["right"] > 0
    finally:
        controller.close()
    assert all(value == 0.0 for value in backend.levels.values())
