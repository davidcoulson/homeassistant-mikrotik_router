"""SNMP-style interface throughput, counters and link sensors."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    UnitOfDataRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router.const import DOMAIN
from custom_components.mikrotik_router.coordinator import (
    MikrotikCoordinator,
    _counter_delta,
    _duplex,
    _link_speed,
    _parse_routeros_time,
)
from custom_components.mikrotik_router.entity import _skip_sensor
from custom_components.mikrotik_router.helper import format_data_rate
from custom_components.mikrotik_router.sensor import MikrotikInterfaceTrafficSensor
from custom_components.mikrotik_router.sensor_types import SENSOR_TYPES

MONOTONIC = "custom_components.mikrotik_router.coordinator.monotonic"


def _description(key):
    return next(d for d in SENSOR_TYPES if d.key == key)


def _coordinator(rows, port_traffic=True):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.ds = {"interface": {}}
    coordinator._slow_poll = False
    coordinator._monitor_batch = True
    coordinator.interface_traffic_last_run = None
    coordinator.config_entry = MagicMock(options={"sensor_port_traffic": port_traffic})
    coordinator.api = MagicMock()
    coordinator.api.connected.return_value = True
    coordinator.api.query.side_effect = (
        lambda path, **kw: rows if path == "/interface" else None
    )
    return coordinator


def _row(**counters):
    return {
        "default-name": "ether1",
        "name": "ether1",
        "type": "ether",
        ".id": "*1",
        "rx-byte": 0,
        "tx-byte": 0,
        "rx-packet": 0,
        "tx-packet": 0,
        "rx-error": 0,
        "tx-error": 0,
        "rx-drop": 0,
        "tx-drop": 0,
        "tx-queue-drop": 0,
        **counters,
    }


def _poll(coordinator, at):
    with patch(MONOTONIC, return_value=at):
        coordinator.get_interface()
    return coordinator.ds["interface"]["ether1"]


def test_throughput_is_counter_delta_over_window() -> None:
    """Rate = byte delta * 8 / elapsed, published about once a minute."""
    rows = [_row(**{"rx-byte": 1_000_000, "tx-byte": 500_000, "rx-error": 3})]
    coordinator = _coordinator(rows)

    vals = _poll(coordinator, 1000.0)
    # The first window has no previous counters: no rate yet, counters at once
    assert vals["rx"] is None and vals["tx"] is None
    assert vals["rx-total"] == 1_000_000
    assert vals["rx-errors"] == 3

    # A 30 s poll inside the window changes nothing
    rows[0].update({"rx-byte": 4_000_000, "tx-byte": 800_000, "rx-error": 5})
    vals = _poll(coordinator, 1030.0)
    assert vals["rx"] is None
    assert vals["rx-total"] == 1_000_000
    assert vals["rx-errors"] == 3

    # The poll that closes the window (slightly early is fine)
    rows[0].update({"rx-byte": 8_500_000, "tx-byte": 1_250_000, "rx-error": 6})
    vals = _poll(coordinator, 1059.5)
    assert vals["rx"] == 1_010_000  # 1,008,403 bit/s to 3 significant figures
    assert vals["tx"] == 101_000
    assert vals["rx-total"] == 8_500_000
    assert vals["rx-errors"] == 6
    assert vals["rx-packets"] == 0


def test_counter_reset_does_not_spike() -> None:
    """A counter that went backwards (reboot, reset-counters, wrap) counts
    from zero instead of producing a huge or negative rate."""
    rows = [_row(**{"rx-byte": 10_000_000_000})]
    coordinator = _coordinator(rows)
    _poll(coordinator, 0.0)
    rows[0]["rx-byte"] = 600_000
    vals = _poll(coordinator, 60.0)
    assert vals["rx"] == 80_000  # 600 kB in 60 s
    assert vals["rx-total"] == 600_000


def test_counter_delta() -> None:
    assert _counter_delta(150, 100) == 50
    assert _counter_delta(20, 2**32 - 10) == 20
    assert _counter_delta(None, 100) is None
    assert _counter_delta(100, None) is None


def test_missing_counters_stay_unknown() -> None:
    """Interfaces without error counters don't get fake zeros."""
    rows = [_row()]
    del rows[0]["rx-error"]
    coordinator = _coordinator(rows)
    vals = _poll(coordinator, 0.0)
    assert vals["rx-errors"] is None
    assert _skip_sensor(
        coordinator.config_entry, _description("rx-errors"), coordinator.ds["interface"], "ether1"
    )
    assert not _skip_sensor(
        coordinator.config_entry, _description("tx-errors"), coordinator.ds["interface"], "ether1"
    )


def _switch_chip_row(**counters):
    """A CRS3xx switch port: /interface has no error or drop counters."""
    row = _row(**counters)
    for field in ("rx-error", "tx-error", "rx-drop", "tx-drop"):
        del row[field]
    return row


def _with_ethernet_stats(coordinator, rows, stats):
    calls = []

    def query(path, command=None, **kw):
        if path == "/interface":
            return rows
        if path == "/interface/ethernet" and command == "print":
            calls.append(kw)
            return stats
        return None

    coordinator.api.query.side_effect = query
    return calls


def test_switch_chip_errors_from_ethernet_stats() -> None:
    """Error and drop counters missing from /interface come from the
    ethernet stats, summed over the causes the router reports, once per
    traffic window."""
    rows = [_switch_chip_row()]
    coordinator = _coordinator(rows)
    stats = [
        {
            "name": "ether1",
            "rx-fcs-error": 2,
            "rx-align-error": 1,
            "rx-too-long": 4,
            "tx-underrun": 0,
            "rx-drop": 5,
            "rx-overflow": 1,
            "tx-drop": 7,
            "rx-pause": 99,
        },
        {"name": "sfp-sfpplus9", "rx-fcs-error": 50},
    ]
    calls = _with_ethernet_stats(coordinator, rows, stats)

    vals = _poll(coordinator, 0.0)
    assert vals["rx-errors"] == 7
    assert vals["tx-errors"] == 0
    assert vals["rx-drops"] == 6
    assert vals["tx-drops"] == 7
    assert calls == [{"args": {"stats": ""}, "ignore_trap": True}]
    assert not _skip_sensor(
        coordinator.config_entry, _description("rx-errors"), coordinator.ds["interface"], "ether1"
    )

    # Inside the window: no extra call, values held
    stats[0]["rx-fcs-error"] = 10
    vals = _poll(coordinator, 30.0)
    assert len(calls) == 1
    assert vals["rx-errors"] == 7

    vals = _poll(coordinator, 60.0)
    assert len(calls) == 2
    assert vals["rx-errors"] == 15


def test_ethernet_stats_not_needed_or_refused() -> None:
    """Ports that report their own counters cost no extra call; a router
    that refuses the stats isn't asked again and the sensors stay skipped."""
    rows = [_row(**{"rx-error": 3})]
    coordinator = _coordinator(rows)
    calls = _with_ethernet_stats(coordinator, rows, [{"name": "ether1", "rx-fcs-error": 9}])
    assert _poll(coordinator, 0.0)["rx-errors"] == 3
    assert not calls

    rows = [_switch_chip_row()]
    coordinator = _coordinator(rows)
    calls = _with_ethernet_stats(coordinator, rows, None)
    assert _poll(coordinator, 0.0)["rx-errors"] is None
    _poll(coordinator, 60.0)
    assert len(calls) == 1
    assert not coordinator._ethernet_stats_supported


def test_ethernet_stats_lost_connection_retries() -> None:
    """A failed call on a lost connection is retried in a later window."""
    rows = [_switch_chip_row()]
    coordinator = _coordinator(rows)
    _with_ethernet_stats(coordinator, rows, None)
    coordinator.ds["interface"] = {"ether1": {"name": "ether1", "type": "ether"}}
    coordinator.api.connected.return_value = False
    coordinator._fill_ethernet_error_stats()
    assert coordinator._ethernet_stats_supported


def test_no_traffic_without_option() -> None:
    rows = [_row(**{"rx-byte": 1000})]
    coordinator = _coordinator(rows, port_traffic=False)
    vals = _poll(coordinator, 0.0)
    assert vals["rx-total"] is None
    assert coordinator.interface_traffic_last_run is None


def test_failed_query_keeps_window() -> None:
    coordinator = _coordinator(None)
    coordinator.interface_traffic_last_run = 123.0
    with patch(MONOTONIC, return_value=500.0):
        coordinator.get_interface()
    assert coordinator.interface_traffic_last_run == 123.0


@pytest.mark.parametrize(
    ("rate", "speed"),
    [
        ("10Mbps", 10),
        ("100Mbps", 100),
        ("1Gbps", 1000),
        ("2.5Gbps", 2500),
        ("10Gbps", 10000),
        ("unknown", None),
        (None, None),
    ],
)
def test_link_speed(rate, speed) -> None:
    assert _link_speed(rate) == speed


def test_duplex() -> None:
    assert _duplex(True) == "full"
    assert _duplex(False) == "half"
    assert _duplex("unknown") is None


def test_parse_routeros_time() -> None:
    tz = dt_util.get_default_time_zone()
    assert _parse_routeros_time("2024-01-05 10:22:33") == datetime(
        2024, 1, 5, 10, 22, 33, tzinfo=tz
    )
    assert _parse_routeros_time("jan/05/2024 10:22:33") == datetime(
        2024, 1, 5, 10, 22, 33, tzinfo=tz
    )
    assert _parse_routeros_time("") is None
    assert _parse_routeros_time("garbage") is None


def test_link_fields_from_monitor() -> None:
    """Link speed, duplex and the latest link change come with every poll."""
    rows = [
        _row(
            **{
                "last-link-up-time": "2024-01-05 10:22:33",
                "last-link-down-time": "2024-01-05 10:20:00",
            }
        )
    ]
    coordinator = _coordinator(rows)
    monitor = [{"name": "ether1", "status": "link-ok", "rate": "10Gbps", "full-duplex": True}]
    coordinator.api.query.side_effect = lambda path, **kw: (
        monitor if kw.get("command") == "monitor" else rows if path == "/interface" else None
    )
    vals = _poll(coordinator, 0.0)
    assert vals["link-speed"] == 10000
    assert vals["duplex"] == "full"
    assert vals["last-link-change"] == datetime(
        2024, 1, 5, 10, 22, 33, tzinfo=dt_util.get_default_time_zone()
    )


@pytest.mark.parametrize(
    ("bits", "text"),
    [
        (0, "0 bit/s"),
        (950, "950 bit/s"),
        (12_340, "12.3 kbit/s"),
        (12_340_000, "12.3 Mbit/s"),
        (1_500_000_000, "1.5 Gbit/s"),
        (None, None),
    ],
)
def test_format_data_rate(bits, text) -> None:
    assert format_data_rate(bits) == text


def test_throughput_sensor_units_and_formatted_attribute() -> None:
    description = _description("traffic_rx")
    assert description.native_unit_of_measurement == UnitOfDataRate.BITS_PER_SECOND
    assert description.suggested_unit_of_measurement == UnitOfDataRate.MEGABITS_PER_SECOND
    assert description.device_class == SensorDeviceClass.DATA_RATE

    coordinator = MagicMock()
    coordinator.config_entry.data = {CONF_NAME: "Mikrotik"}
    row = {
        "default-name": "ether1",
        "name": "ether1",
        "type": "vlan",
        "comment": "",
        "rx": 12_300_000,
    }
    coordinator.data = {"interface": {"ether1": row}}
    sensor = MikrotikInterfaceTrafficSensor(coordinator, description, "ether1")
    assert sensor.native_value == 12_300_000
    assert sensor.extra_state_attributes["formatted"] == "12.3 Mbit/s"
    assert "formatted" in sensor._Entity__combined_unrecorded_attributes


def test_link_speed_only_for_ethernet() -> None:
    entry = MagicMock(options={"sensor_port_traffic": True})
    data = {"vlan10": {"type": "vlan", "link-speed": None}}
    assert _skip_sensor(entry, _description("link-speed"), data, "vlan10")
    data = {"ether1": {"type": "ether", "link-speed": None}}
    assert not _skip_sensor(entry, _description("link-speed"), data, "ether1")


# ---------------------------------------------------------------------------
# Setup: which entities exist and which are enabled
# ---------------------------------------------------------------------------
SERIAL = "HEX123"


async def _fake_update(self):
    self.ds["access"] = ["write", "policy", "reboot", "test"]
    self.ds["routerboard"] = {"routerboard": True, "serial-number": SERIAL, "model": "CRS305"}
    self.ds["resource"] = {"board-name": "CRS305", "platform": "MikroTik", "version": "7.20.1"}
    self.ds["interface"] = {
        "sfp-sfpplus1": {
            "default-name": "sfp-sfpplus1",
            "name": "sfp-sfpplus1",
            "type": "ether",
            "running": True,
            "enabled": True,
            "port-mac-address": "AA:BB:CC:DD:EE:02",
            "comment": "",
            "client-ip-address": "",
            "client-mac-address": "",
            "rx": 1_000_000,
            "tx": 2_000_000,
            "rx-total": 10,
            "tx-total": 20,
            "rx-packets": 1,
            "tx-packets": 2,
            "rx-errors": 0,
            "tx-errors": 0,
            "rx-drops": 0,
            "tx-drops": 0,
            "tx-queue-drops": 0,
            "link-downs": 1,
            "link-speed": 10000,
            "duplex": "full",
            "last-link-change": datetime(2024, 1, 5, 10, 22, 33, tzinfo=dt_util.UTC),
        }
    }
    return self.ds


async def _fake_tracker_update(self):
    return None


async def test_interface_sensors_created(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=3,
        unique_id=f"routerboard:{SERIAL.lower()}",
        data={
            CONF_NAME: "Mikrotik",
            CONF_HOST: "192.0.2.1",
            CONF_PORT: 0,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
        },
        options={"sensor_port_traffic": True},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikCoordinator._async_update_data",
            _fake_update,
        ),
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikTrackerCoordinator._async_update_data",
            _fake_tracker_update,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entries = {
        (e.domain, e.unique_id): e
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }

    def enabled(domain, key):
        return entries[(domain, f"mikrotik-{key}-sfp_sfpplus1")].disabled_by is None

    # Existing unique ids are unchanged
    for key in ("traffic_rx", "traffic_tx", "rx-total", "tx-total"):
        assert enabled("sensor", key)
    for key in ("rx-errors", "tx-errors", "rx-drops", "tx-drops", "link-speed", "last-link-change"):
        assert enabled("sensor", key)
    for key in ("rx-packets", "tx-packets", "tx-queue-drops", "link-downs", "duplex"):
        assert not enabled("sensor", key)
    # Link status comes with the port traffic option, without the port tracker
    assert enabled("binary_sensor", "interface")

    rx = hass.states.get(entries[("sensor", "mikrotik-traffic_rx-sfp_sfpplus1")].entity_id)
    assert rx.attributes["unit_of_measurement"] == UnitOfDataRate.MEGABITS_PER_SECOND
    assert float(rx.state) == 1.0
    assert rx.attributes["formatted"] == "1 Mbit/s"
    speed = hass.states.get(entries[("sensor", "mikrotik-link-speed-sfp_sfpplus1")].entity_id)
    assert speed.state == "10000"
