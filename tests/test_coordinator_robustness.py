"""Tests for coordinator robustness fixes (items 11, 14, 15)."""

from ipaddress import IPv4Network
from unittest.mock import MagicMock

from custom_components.mikrotik_router.coordinator import (
    MikrotikCoordinator,
    _split_pair,
)


def _coordinator(responses=None):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.ds = {
        "interface": {},
        "queue": {},
        "wireless": {},
        "host": {},
        "client_traffic": {},
        "dhcp-network": {},
    }
    coordinator.notified_flags = []
    coordinator.api = MagicMock()
    coordinator.api.connected.return_value = True
    responses = responses or {}
    coordinator.api.query.side_effect = lambda path, **kw: responses.get(path)
    return coordinator


def test_split_pair_tolerates_bad_values() -> None:
    assert _split_pair("1000/2000", int) == (1000, 2000)
    assert _split_pair("1k/2000", int) == (0, 0)
    assert _split_pair("1000", int) == (0, 0)
    assert _split_pair("1s/2s", str, "0s") == ("1s", "2s")


def test_queue_with_unexpected_format_does_not_fail() -> None:
    coordinator = _coordinator(
        {
            "/queue/simple": [
                {".id": "*1", "name": "q", "max-limit": "10M/20M", "rate": "5/6"}
            ]
        }
    )
    coordinator.get_queue()
    queue = coordinator.ds["queue"]["q"]
    assert queue["upload-max-limit"] == "0 bps"
    assert queue["upload-rate"] == "5 bps"
    assert queue["download-rate"] == "6 bps"
    assert queue["upload-burst-time"] == "0s"


def test_wireless_unknown_master_and_two_modules() -> None:
    coordinator = _coordinator(
        {
            "/interface/wifi": [{"name": "wifi1", "ssid": "a"}],
            "/interface/wireless": [
                {"name": "wlan2", "master-interface": "missing"}
            ],
        }
    )
    coordinator._wifimodules = ["wifi", "wireless"]
    coordinator.get_wireless()
    assert set(coordinator.ds["wireless"]) == {"wifi1", "wlan2"}


def test_traffic_timer_not_advanced_on_failed_query() -> None:
    coordinator = _coordinator()
    coordinator.config_entry = MagicMock(options={"sensor_port_traffic": True})
    coordinator.interface_traffic_last_run = 123.0
    coordinator.get_interface()
    assert coordinator.interface_traffic_last_run == 123.0


def test_accounting_follows_address_changes() -> None:
    coordinator = _coordinator(
        {
            "/ip/accounting/snapshot": [
                {".id": "*1", "src-address": "10.0.0.2", "dst-address": "8.8.8.8",
                 "bytes": 1000},
                {".id": "*2", "src-address": "bogus", "dst-address": "10.0.0.2",
                 "bytes": 5},
            ]
        }
    )
    coordinator.ds["dhcp-network"] = {
        "n": {"IPv4Network": IPv4Network("10.0.0.0/24")}
    }
    coordinator.api.is_accounting_and_local_traffic_enabled.return_value = (
        True,
        False,
    )
    coordinator.api.take_client_traffic_snapshot.return_value = 10.0
    coordinator.api.accounting_threshold = 0
    coordinator.ds["host"] = {
        "A": {"address": "10.0.0.1", "mac-address": "A", "host-name": "a"},
        "B": {"address": "unknown", "mac-address": "B", "host-name": "b"},
        "C": {"address": "unknown", "mac-address": "C", "host-name": "c"},
    }
    coordinator.process_accounting()
    # A moves to a new address after a DHCP change
    coordinator.ds["host"]["A"]["address"] = "10.0.0.2"
    coordinator.process_accounting()
    assert coordinator.ds["client_traffic"]["A"]["address"] == "10.0.0.2"
    assert coordinator.ds["client_traffic"]["A"]["wan-tx"] == 100
    assert "wan-tx" not in coordinator.ds["client_traffic"]["B"]


def test_dhcp_network_with_host_bits() -> None:
    coordinator = _coordinator(
        {"/ip/dhcp-server/network": [{"address": "10.0.0.1/24"}]}
    )
    coordinator.get_dhcp_network()
    (network,) = coordinator.ds["dhcp-network"].values()
    assert network["IPv4Network"] == IPv4Network("10.0.0.0/24")


def test_cpu_load_and_temperature_hysteresis(monkeypatch) -> None:
    """P1: small moves are held back until they get old."""
    from custom_components.mikrotik_router import coordinator as module

    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    coordinator = _coordinator()
    coordinator._published = {}
    coordinator.ds["resource"] = {}
    coordinator.ds["health"] = {}

    def publish(path, key, value):
        coordinator.ds[path][key] = value
        coordinator._apply_hysteresis(path, key)
        return coordinator.ds[path][key]

    assert publish("resource", "cpu-load", 10) == 10
    assert publish("resource", "cpu-load", 11) == 10
    assert publish("resource", "cpu-load", 12) == 12
    assert publish("health", "cpu-temperature", "45") == "45"
    assert publish("health", "cpu-temperature", "46") == "45"
    clock[0] += 301
    assert publish("health", "cpu-temperature", "46") == "46"
    assert publish("resource", "cpu-load", "unknown") == "unknown"
    assert publish("resource", "cpu-load", 13) == 13


def _monitor_coordinator(batch_ok):
    calls = []

    def query(path, command=None, args=None, **kw):
        calls.append(args)
        if path == "/interface":
            return [
                {"default-name": f"ether{i}", "name": f"ether{i}", ".id": f"*{i}",
                 "type": "ether"}
                for i in (1, 2, 3)
            ]
        if path == "/interface/ethernet" and command == "monitor":
            if "numbers" in args:
                if not batch_ok:
                    return None
                return [{"name": f"ether{i}", "status": "link-ok"} for i in (1, 2, 3)]
            return [{"name": f"ether{args['.id'][1:]}", "status": "no-link"}]
        return None

    coordinator = _coordinator()
    coordinator.api.query.side_effect = query
    coordinator.config_entry = MagicMock(options={})
    coordinator.interface_traffic_last_run = None
    coordinator._monitor_batch = True
    return coordinator, calls


def test_ethernet_monitor_is_batched() -> None:
    """P2: one monitor call for all ports."""
    coordinator, calls = _monitor_coordinator(batch_ok=True)
    coordinator.get_interface()
    monitor = [c for c in calls if c and "once" in c]
    assert monitor == [{"numbers": "*1,*2,*3", "once": True}]
    assert coordinator.ds["interface"]["ether2"]["status"] == "link-ok"


def test_ethernet_monitor_falls_back_per_port() -> None:
    coordinator, calls = _monitor_coordinator(batch_ok=False)
    coordinator.get_interface()
    assert coordinator._monitor_batch is False
    assert coordinator.ds["interface"]["ether3"]["status"] == "no-link"
    calls.clear()
    coordinator.get_interface()
    assert not [c for c in calls if c and "numbers" in c]
