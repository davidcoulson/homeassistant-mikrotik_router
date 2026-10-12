"""P4: SFP DDM and wireless signal are separate, disabled diagnostic sensors."""

from unittest.mock import MagicMock

import pytest

from custom_components.mikrotik_router.device_tracker import MikrotikHostDeviceTracker
from custom_components.mikrotik_router.device_tracker_types import (
    DEVICE_ATTRIBUTES_HOST,
)
from custom_components.mikrotik_router.entity import _skip_sensor
from custom_components.mikrotik_router.sensor import MikrotikDiagnosticSensor
from custom_components.mikrotik_router.sensor_types import (
    DEVICE_ATTRIBUTES_IFACE_SFP,
    SENSOR_TYPES,
)

DIAGNOSTIC = {
    d.key: d for d in SENSOR_TYPES if d.func == "MikrotikDiagnosticSensor"
}


def test_descriptions_are_disabled_diagnostics() -> None:
    assert set(DIAGNOSTIC) == {
        "sfp_temperature",
        "sfp_supply_voltage",
        "sfp_tx_bias_current",
        "sfp_tx_power",
        "sfp_rx_power",
        "host_signal_strength",
        "host_tx_ccq",
        "host_tx_rate",
        "host_rx_rate",
    }
    for description in DIAGNOSTIC.values():
        assert description.entity_registry_enabled_default is False
        assert description.entity_category == "diagnostic"
    assert "sfp-rx-power" not in DEVICE_ATTRIBUTES_IFACE_SFP
    assert "signal-strength" not in DEVICE_ATTRIBUTES_HOST
    assert "last_seen" in MikrotikHostDeviceTracker._unrecorded_attributes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-65, -65),
        ("-65@6Mbps", -65.0),
        ("433.3Mbps-80MHz/2S/SGI", 433.3),
        ("-3.01", -3.01),
        ("unknown", None),
        (True, None),
        (None, None),
    ],
)
def test_native_value_parses_leading_number(value, expected) -> None:
    sensor = object.__new__(MikrotikDiagnosticSensor)
    sensor.entity_description = DIAGNOSTIC["host_signal_strength"]
    sensor._data = {"signal-strength": value}
    assert sensor.native_value == expected


def test_skip_rules() -> None:
    entry = MagicMock(options={"track_network_hosts": True})
    sfp = DIAGNOSTIC["sfp_rx_power"]
    data = {
        "sfp1": {"sfp-rx-power": "-5.1", "sfp-module-present": True},
        "sfp2": {"sfp-rx-power": 0, "sfp-module-present": False},
        "ether1": {"status": "link-ok"},
    }
    assert not _skip_sensor(entry, sfp, data, "sfp1")
    assert _skip_sensor(entry, sfp, data, "sfp2")
    assert _skip_sensor(entry, sfp, data, "ether1")

    signal = DIAGNOSTIC["host_signal_strength"]
    hosts = {"aa": {"signal-strength": "-60"}, "bb": {"address": "10.0.0.2"}}
    assert not _skip_sensor(entry, signal, hosts, "aa")
    assert _skip_sensor(entry, signal, hosts, "bb")
    no_tracking = MagicMock(options={"track_network_hosts": False})
    assert _skip_sensor(no_tracking, signal, hosts, "aa")
