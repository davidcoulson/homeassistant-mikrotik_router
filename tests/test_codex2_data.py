"""Review 2: data correctness fixes (SFP temperature, client traffic, ...)."""

from unittest.mock import MagicMock

from custom_components.mikrotik_router.coordinator import MikrotikCoordinator
from custom_components.mikrotik_router.helper import is_sfp_port
from custom_components.mikrotik_router.sensor import MikrotikDiagnosticSensor
from custom_components.mikrotik_router.sensor_types import SENSOR_TYPES


def _coordinator(interfaces):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.ds = {"interface": interfaces}
    coordinator.api = MagicMock()
    coordinator._monitor_batch = True
    return coordinator


# M7: copper ports and empty cages report no SFP temperature
def test_ethernet_config_leaves_copper_ports_without_sfp_marker() -> None:
    coordinator = _coordinator(
        {
            "ether1": {"name": "ether1", "default-name": "ether1", "type": "ether"},
            "sfp1": {"name": "sfp1", "default-name": "sfp1", "type": "ether"},
        }
    )
    coordinator.api.query.return_value = [
        {"default-name": "ether1", "name": "ether1"},
        {"default-name": "sfp1", "name": "sfp1", "sfp-shutdown-temperature": 95},
    ]
    coordinator._get_ethernet_config()
    assert coordinator.ds["interface"]["ether1"]["sfp-shutdown-temperature"] is None
    assert coordinator.ds["interface"]["sfp1"]["sfp-shutdown-temperature"] == 95
    assert not is_sfp_port(coordinator.ds["interface"]["ether1"])
    assert is_sfp_port(coordinator.ds["interface"]["sfp1"])


def test_monitor_without_module_reports_none_temperature() -> None:
    from custom_components.mikrotik_router.coordinator import ETHERNET_MONITOR_SFP_VALS

    coordinator = _coordinator(
        {"sfp1": {"name": "sfp1", "default-name": "sfp1", "type": "ether"}}
    )
    # An empty cage: no sfp-* fields at all in the monitor reply
    coordinator.api.query.return_value = [{"name": "sfp1", "status": "no-link"}]
    coordinator._monitor_ethernet(["*1"], ETHERNET_MONITOR_SFP_VALS)
    assert coordinator.ds["interface"]["sfp1"]["sfp-temperature"] is None

    sensor = object.__new__(MikrotikDiagnosticSensor)
    sensor.entity_description = next(d for d in SENSOR_TYPES if d.key == "sfp_temperature")
    sensor._data = coordinator.ds["interface"]["sfp1"]
    assert sensor.native_value is None

    coordinator.api.query.return_value = [
        {"name": "sfp1", "status": "link-ok", "sfp-temperature": 41}
    ]
    coordinator._monitor_ethernet(["*1"], ETHERNET_MONITOR_SFP_VALS)
    sensor._data = coordinator.ds["interface"]["sfp1"]
    assert sensor.native_value == 41


# M4/M5: kid-control client traffic rates
def _kid_control(monkeypatch):
    from custom_components.mikrotik_router import coordinator as module

    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.ds = {
        "host": {"A": {"address": "10.0.0.1", "mac-address": "A", "host-name": "a"}},
        "client_traffic": {},
    }
    coordinator.notified_flags = []
    coordinator.api = MagicMock()
    coordinator.api.take_client_traffic_snapshot.return_value = 0
    return coordinator, clock


def _reply(coordinator, devices):
    coordinator.api.query.return_value = [
        {
            "mac-address": mac,
            "bytes-up": up,
            "bytes-down": down,
            "disabled": False,
        }
        for mac, up, down in devices
    ]
    coordinator.process_kid_control_devices()


def test_rate_after_missed_sample_uses_the_devices_own_baseline(monkeypatch) -> None:
    """M4: 1000 B at t=0, nothing at t=60, 13000 B at t=120 is 100 B/s."""
    coordinator, clock = _kid_control(monkeypatch)
    _reply(coordinator, [("A", 1000, 1000)])
    assert coordinator.ds["client_traffic"]["A"]["tx"] is None

    clock[0] += 60
    _reply(coordinator, [])  # the router returned no devices
    clock[0] += 60
    _reply(coordinator, [("A", 13000, 13000)])
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 100
    assert coordinator.ds["client_traffic"]["A"]["rx"] == 100


def test_device_absent_from_one_reply_keeps_its_baseline(monkeypatch) -> None:
    coordinator, clock = _kid_control(monkeypatch)
    coordinator.ds["host"]["B"] = {"address": "10.0.0.2", "mac-address": "B", "host-name": "b"}
    _reply(coordinator, [("A", 1000, 0), ("B", 0, 0)])
    clock[0] += 60
    _reply(coordinator, [("B", 6000, 0)])  # A is missing
    clock[0] += 60
    _reply(coordinator, [("A", 13000, 0), ("B", 12000, 0)])
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 100
    assert coordinator.ds["client_traffic"]["B"]["tx"] == 100


def test_new_client_does_not_spike_with_its_lifetime_counter(monkeypatch) -> None:
    """M5: a client discovered after the clock started has no rate yet."""
    coordinator, clock = _kid_control(monkeypatch)
    _reply(coordinator, [("A", 10, 10)])
    clock[0] += 60
    coordinator.ds["host"]["B"] = {"address": "10.0.0.2", "mac-address": "B", "host-name": "b"}
    _reply(coordinator, [("A", 70, 70), ("B", 50_000_000_000, 50_000_000_000)])
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 1
    assert coordinator.ds["client_traffic"]["B"]["tx"] is None
    assert coordinator.ds["client_traffic"]["B"]["rx"] is None

    clock[0] += 60
    _reply(coordinator, [("A", 130, 130), ("B", 50_000_006_000, 50_000_006_000)])
    assert coordinator.ds["client_traffic"]["B"]["tx"] == 100


def test_counter_reset_and_quick_refresh(monkeypatch) -> None:
    coordinator, clock = _kid_control(monkeypatch)
    _reply(coordinator, [("A", 5000, 5000)])
    clock[0] += 60
    _reply(coordinator, [("A", 600, 600)])  # reset: counts from zero
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 10
    clock[0] += 0.2
    _reply(coordinator, [("A", 700, 700)])  # forced refresh: no new rate
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 10
    clock[0] += 59.8  # 60 s after the last real sample
    _reply(coordinator, [("A", 1200, 1200)])
    assert coordinator.ds["client_traffic"]["A"]["tx"] == 10
