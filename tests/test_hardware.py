import pytest
from conftest import FakeI2CBus

from ane_haptic_remote.config import HardwareConfig
from ane_haptic_remote.controller import HapticController
from ane_haptic_remote.hardware import (
    HardwareNotFound,
    build_backend,
    detect_wiring,
    intensity_to_rtp,
    od_clamp_for,
    scan_bus,
)

DRV = 0x5A
MUX = 0x70


def three_driver_bus():
    return FakeI2CBus(main=[MUX], channels={4: [DRV], 5: [DRV], 6: [DRV]})


def single_driver_bus(channel=4):
    return FakeI2CBus(main=[MUX], channels={channel: [DRV]})


def test_scan_reports_the_mux_and_each_channel():
    bus = three_driver_bus()
    report = scan_bus(bus, HardwareConfig())
    assert report["main"] == [MUX]
    assert report["ch4"] == [DRV]
    assert report["ch5"] == [DRV]
    assert report["ch6"] == [DRV]
    assert report["ch0"] == []


def test_scan_without_a_mux():
    bus = FakeI2CBus(main=[DRV])
    report = scan_bus(bus, HardwareConfig())
    assert report == {"main": [DRV]}


def test_detect_three_drivers():
    wiring = detect_wiring(three_driver_bus(), HardwareConfig())
    assert wiring.topology == "three_drv"
    assert wiring.channels == {"left": 4, "middle": 5, "right": 6}
    assert "3x DRV2605L" in wiring.describe(HardwareConfig())


def test_detect_single_driver_behind_the_mux():
    wiring = detect_wiring(single_driver_bus(4), HardwareConfig(single_mux_channel=4))
    assert wiring.topology == "single_drv"
    assert wiring.channels == {"all": 4}


def test_detect_single_driver_scans_channels_when_not_configured():
    wiring = detect_wiring(single_driver_bus(6), HardwareConfig())
    assert wiring.channels == {"all": 6}


def test_detect_single_driver_on_the_main_bus():
    wiring = detect_wiring(FakeI2CBus(main=[DRV]), HardwareConfig())
    assert wiring.topology == "single_drv"
    assert wiring.channels == {"all": None}


def test_detect_forces_three_drivers_and_fails_loudly():
    with pytest.raises(HardwareNotFound):
        detect_wiring(single_driver_bus(4), HardwareConfig(), topology="three_drv")


def test_detect_reports_missing_hardware():
    with pytest.raises(HardwareNotFound):
        detect_wiring(FakeI2CBus(), HardwareConfig())


def test_od_clamp_matches_the_erm_voltage():
    assert od_clamp_for(3.0) == 142
    assert od_clamp_for(0.0) == 0
    assert od_clamp_for(99.0) == 255


def test_intensity_to_rtp():
    assert intensity_to_rtp(0.0, 0.3) == 0
    assert intensity_to_rtp(1.0, 0.3) == 127
    assert intensity_to_rtp(0.01, 0.3) == int(round(0.307 * 127))
    floor = intensity_to_rtp(0.1, 0.3)
    assert floor >= 39


def test_build_backend_initialises_three_drivers():
    bus = three_driver_bus()
    backend, wiring = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    assert wiring.topology == "three_drv"
    for channel in (4, 5, 6):
        assert bus.register(channel, DRV, 0x01) == 0x05
        assert bus.register(channel, DRV, 0x17) == 142
        assert bus.register(channel, DRV, 0x02) == 0
    backend.close()
    assert bus.closed is True


def test_left_only_reaches_the_left_driver():
    bus = three_driver_bus()
    backend, _ = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    backend.write({"left": 0.9})
    assert bus.register(4, DRV, 0x02) > 0
    assert bus.register(5, DRV, 0x02) == 0
    assert bus.register(6, DRV, 0x02) == 0
    backend.write({"middle": 0.9})
    assert bus.register(4, DRV, 0x02) == 0
    assert bus.register(5, DRV, 0x02) > 0
    backend.stop_all()
    assert all(bus.register(channel, DRV, 0x02) == 0 for channel in (4, 5, 6))


def test_right_only_reaches_the_right_driver():
    bus = three_driver_bus()
    backend, _ = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    backend.write({"right": 1.0})
    assert bus.register(6, DRV, 0x02) > 0
    assert bus.register(4, DRV, 0x02) == 0
    assert bus.register(5, DRV, 0x02) == 0


def test_backend_skips_redundant_writes():
    bus = three_driver_bus()
    backend, _ = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    backend.write({"left": 0.9})
    writes = len(bus.writes)
    backend.write({"left": 0.9})
    assert len(bus.writes) == writes


def test_shared_backend_takes_the_peak_of_all_motors():
    bus = single_driver_bus()
    backend, wiring = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    assert wiring.topology == "single_drv"
    backend.write({"left": 0.2, "middle": 0.9, "right": 0.0})
    assert bus.register(4, DRV, 0x02) == intensity_to_rtp(0.9, 0.3)
    backend.close()


def test_controller_against_three_real_drivers_taps_the_right_motor():
    bus = three_driver_bus()
    backend, _ = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    controller = HapticController(backend, HardwareConfig(), clock=lambda: 0.0)
    controller.hold(["middle"], intensity=0.8, duration_s=3.0)
    backend.write(controller.render(0.0))
    assert bus.register(5, DRV, 0x02) > 0
    assert bus.register(4, DRV, 0x02) == 0
    assert bus.register(6, DRV, 0x02) == 0
    controller.stop()
    backend.write(controller.render(0.0))
    assert all(bus.register(channel, DRV, 0x02) == 0 for channel in (4, 5, 6))


def test_i2c_errors_are_counted_not_raised():
    bus = three_driver_bus()
    backend, _ = build_backend(HardwareConfig(), "auto", bus_factory=lambda _: bus)
    bus.channels[4].clear()
    backend.write({"left": 0.9})
    assert backend.health()["errors"] >= 1
    backend.write({"right": 0.5})
    assert bus.register(6, DRV, 0x02) > 0


def test_simulate_backend_needs_no_hardware():
    backend, wiring = build_backend(HardwareConfig(), "simulate", bus_factory=lambda _: None)
    assert wiring.topology == "simulate"
    backend.write({"left": 0.5})
    assert backend.levels["left"] == 0.5
    backend.close()
