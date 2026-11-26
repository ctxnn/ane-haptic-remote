import pytest

from ane_haptic_remote.config import MOTORS, HardwareConfig
from ane_haptic_remote.patterns import (
    PATTERNS,
    SEQUENCES,
    build_sequence,
    encode_for_shared,
    frame_at,
    pattern_gain,
    tap_cycle,
    timeline_length,
)


def test_steady_is_always_on():
    assert [pattern_gain("steady", t) for t in (0.0, 0.1, 5.0)] == [1.0, 1.0, 1.0]


def test_pulse_blinks():
    assert pattern_gain("pulse", 0.01) == 1.0
    assert pattern_gain("pulse", 0.30) == 0.0
    assert pattern_gain("pulse", 0.60) == 1.0


def test_heartbeat_has_two_beats():
    assert pattern_gain("heartbeat", 0.0) == 1.0
    assert pattern_gain("heartbeat", 0.20) == 0.0
    assert pattern_gain("heartbeat", 0.25) == 1.0
    assert pattern_gain("heartbeat", 0.40) == 0.0


def test_heartbeat_beats_are_short():
    values = [pattern_gain("heartbeat", t / 200) for t in range(200)]
    beats, previous = 0, 0.0
    for value in values:
        if value > 0 and previous == 0:
            beats += 1
        previous = value
    assert beats == 2
    assert sum(1 for value in values if value > 0) / len(values) == pytest.approx(0.24, abs=0.02)


def test_ramp_stays_within_range_and_never_hits_zero():
    values = [pattern_gain("ramp", t / 100) for t in range(200)]
    assert all(0.29 <= value <= 1.0 for value in values)
    assert max(values) > 0.95


@pytest.mark.parametrize("name", PATTERNS)
def test_every_pattern_stays_inside_the_unit_interval(name):
    assert all(0.0 <= pattern_gain(name, t / 50) <= 1.0 for t in range(200))


def test_frame_at_walks_a_timeline():
    timeline = (({"left": 1.0}, 0.5), ({}, 0.5))
    assert frame_at(timeline, 0.0) == {"left": 1.0}
    assert frame_at(timeline, 0.49) == {"left": 1.0}
    assert frame_at(timeline, 0.6) == {}
    assert frame_at(timeline, 1.0) is None
    assert frame_at(timeline, 1.1, repeat=True) == {"left": 1.0}
    assert frame_at((), 0.0) is None


@pytest.mark.parametrize("name", SEQUENCES)
def test_shipped_sequences_are_playable(name):
    timeline = build_sequence(name, 0.8)
    assert timeline_length(timeline) > 0
    assert all(set(frame) <= set(MOTORS) for frame, _ in timeline)
    assert any(frame for frame, _ in timeline)


def test_sweep_visits_left_then_middle_then_right():
    timeline = build_sequence("sweep", 0.5, repeat=1)
    order = [next(iter(frame)) for frame, _ in timeline if frame]
    assert order[:3] == ["left", "middle", "right"]


def test_sequence_intensity_is_clamped():
    timeline = build_sequence("test", 5.0)
    assert all(value <= 1.0 for frame, _ in timeline for value in frame.values())


def test_unknown_sequence_is_rejected():
    with pytest.raises(ValueError):
        build_sequence("conga", 0.5)


def test_tap_cycle_encodes_direction_as_a_code():
    config = HardwareConfig()
    left = tap_cycle({"left": 0.8}, config)
    on_steps = [frame for frame, _ in left if frame]
    assert len(on_steps) == 1
    right = tap_cycle({"right": 0.8}, config)
    assert len([frame for frame, _ in right if frame]) == 3


def test_tap_cycle_orders_motors_left_middle_right():
    config = HardwareConfig()
    timeline = tap_cycle({"right": 0.8, "left": 0.8, "middle": 0.0}, config)
    order = [next(iter(frame)) for frame, _ in timeline if frame]
    assert order == ["left", "right", "right", "right"]
    firsts = list(dict.fromkeys(order))
    assert firsts == ["left", "right"]


def test_tap_cycle_length_matches_the_configured_timings():
    config = HardwareConfig()
    timeline = tap_cycle({"left": 0.8}, config)
    expected = config.tap_on_ms / 1000 + config.group_gap_ms / 1000
    assert timeline_length(timeline) == pytest.approx(expected)


def test_shared_encoding_expands_single_motor_steps():
    config = HardwareConfig()
    timeline = build_sequence("sweep", 0.8, repeat=1)
    encoded = encode_for_shared(timeline, config)
    assert len(encoded) > len(timeline)
    on_steps = [frame for frame, _ in encoded if frame]
    assert len(on_steps) == sum(config.taps.values())  # 1 + 2 + 3 taps
    assert [next(iter(frame)) for frame, _ in encoded if frame] == [
        "left",
        "middle",
        "middle",
        "right",
        "right",
        "right",
    ]
