"""Tests for coordinator robustness fixes (items 11, 14, 15)."""

from ipaddress import IPv4Network
from unittest.mock import AsyncMock, MagicMock

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
    coordinator._slow_poll = True
    coordinator._published = {}
    coordinator._monitor_batch = True
    coordinator._dhcp_server_misses = set()
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


async def test_tiered_polling(monkeypatch) -> None:
    """P3, items 7, 13: static config and the firmware check run less often."""
    from datetime import datetime

    from custom_components.mikrotik_router import coordinator as module

    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    coordinator = _coordinator()
    coordinator.config_entry = MagicMock(
        options={"sensor_nat": True, "sensor_scripts": True}
    )
    coordinator.hass = MagicMock()

    async def run(func, *args):
        return func(*args)

    coordinator.hass.async_add_executor_job = run
    coordinator.api.has_reconnected.return_value = False
    coordinator.last_hwinfo_update = datetime.now()
    coordinator.force_full_refresh = True
    coordinator._last_slow_poll = None
    coordinator._last_firmware_check = None
    coordinator.host_hass_recovered = True
    coordinator.data = None
    for attr in ("support_capsman", "support_wireless", "support_ppp",
                 "support_hotspot", "support_ups", "support_gps"):
        setattr(coordinator, attr, False)
    calls = []
    for name in dir(module.MikrotikCoordinator):
        if name.startswith(("get_", "process_")):
            monkeypatch.setattr(
                coordinator, name, lambda name=name: calls.append(name)
            )

    async def process_host():
        calls.append("process_host")

    coordinator.async_process_host = process_host

    await coordinator._async_update_data()
    assert {"get_nat", "get_dhcp_client", "get_script"} <= set(calls)
    assert "get_firmware_update" not in calls  # never during first refresh

    coordinator.data = coordinator.ds
    calls.clear()
    clock[0] += 30
    await coordinator._async_update_data()
    assert "get_nat" not in calls and "get_dhcp_client" not in calls
    assert {"get_script", "get_interface", "get_firmware_update"} <= set(calls)

    calls.clear()
    clock[0] += 30
    coordinator.async_request_refresh = AsyncMock()
    await coordinator.async_request_full_refresh()
    coordinator.async_request_refresh.assert_awaited_once()
    await coordinator._async_update_data()
    assert "get_nat" in calls and "get_firmware_update" not in calls

    calls.clear()
    clock[0] += 300
    await coordinator._async_update_data()
    assert "get_nat" in calls


def test_round_rate() -> None:
    """P5: rates keep 3 significant figures."""
    from custom_components.mikrotik_router.coordinator import _round_rate

    assert _round_rate(12.4) == 12
    assert _round_rate(999.6) == 1000
    assert _round_rate(123456.7) == 123000
    assert _round_rate(1234) == 1230


def test_traffic_totals_follow_slow_tier() -> None:
    """P5: totals are set at once, then only on slow polls."""
    rows = [{"default-name": "ether1", "name": "ether1", "type": "ether",
             "tx-byte": 1000, "rx-byte": 2000}]
    coordinator = _coordinator({"/interface": rows})
    coordinator.config_entry = MagicMock(options={"sensor_port_traffic": True})
    coordinator.interface_traffic_last_run = None
    coordinator._slow_poll = False
    coordinator.get_interface()
    assert coordinator.ds["interface"]["ether1"]["tx-total"] == 1000
    rows[0]["tx-byte"] = 5000
    coordinator.get_interface()
    assert coordinator.ds["interface"]["ether1"]["tx-total"] == 1000
    coordinator._slow_poll = True
    coordinator.get_interface()
    assert coordinator.ds["interface"]["ether1"]["tx-total"] == 5000


async def test_process_host_is_serialised() -> None:
    """Item 3: the two coordinators never run host processing at once."""
    import asyncio

    coordinator = _coordinator()
    coordinator._host_lock = asyncio.Lock()
    running = []
    overlaps = []

    async def body():
        running.append(1)
        overlaps.append(len(running))
        await asyncio.sleep(0)
        running.pop()

    coordinator._async_process_host = body
    await asyncio.gather(
        coordinator.async_process_host(), coordinator.async_process_host()
    )
    assert overlaps == [1, 1]


def test_arp_table_is_swapped_not_resized() -> None:
    """Item 3: readers holding the old table never see it change size."""
    coordinator = _coordinator(
        {"/ip/arp": [{"mac-address": "BB", "address": "10.0.0.3",
                      "interface": "ether2"}]}
    )
    coordinator.ds.update({"bridge": {}, "bridge_host": {}, "dhcp-client": {}})
    old = {"AA": {"mac-address": "AA", "address": "10.0.0.2",
                  "interface": "ether1", "bridge": ""}}
    coordinator.ds["arp"] = old
    coordinator.get_arp()
    assert set(old) == {"AA"}
    assert set(coordinator.ds["arp"]) == {"BB"}


def test_interface_clients_from_arp_index() -> None:
    """Item 21: one ARP index gives the same client columns."""
    coordinator = _coordinator()
    coordinator.config_entry = MagicMock(options={"track_iface_clients": True})
    coordinator.ds.update(
        {
            "interface": {
                "ether1": {"name": "ether1"},
                "ether2": {"name": "ether2"},
                "ether3": {"name": "ether3"},
                "ether4": {"name": "ether4"},
            },
            "arp": {
                "A": {"interface": "ether1", "address": "10.0.0.2",
                      "mac-address": "A"},
                "B": {"interface": "bond1", "address": "10.0.0.3",
                      "mac-address": "B"},
                "C": {"interface": "bond1", "address": "10.0.0.4",
                      "mac-address": "C"},
            },
            "bonding_slaves": {"ether2": {"master": "bond1"}},
            "dhcp-client": {"ether3": {"address": "192.0.2.1"}},
        }
    )
    coordinator.process_interface_client()
    iface = coordinator.ds["interface"]
    assert iface["ether1"]["client-ip-address"] == "10.0.0.2"
    assert iface["ether1"]["client-mac-address"] == "A"
    assert iface["ether2"]["client-ip-address"] == "multiple"
    assert iface["ether3"]["client-ip-address"] == "192.0.2.1"
    assert iface["ether3"]["client-mac-address"] == "none"
    assert iface["ether4"]["client-ip-address"] == "none"
