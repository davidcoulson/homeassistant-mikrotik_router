"""Host tracker coordinator tests."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from custom_components.mikrotik_router.coordinator import MikrotikTrackerCoordinator


def _tracker(hass, hosts, access=("read", "test")):
    tracker = object.__new__(MikrotikTrackerCoordinator)
    tracker.hass = hass
    coordinator = MagicMock()
    coordinator.option_track_network_hosts = True
    coordinator.option_track_network_hosts_timeout = timedelta(seconds=180)
    coordinator.host_tracking_initialized = True
    coordinator.async_process_host = AsyncMock()
    coordinator.ds = {
        "access": list(access),
        "host": hosts,
        "arp": {"m2": {"bridge": "bridge1"}},
        "routerboard": {"serial-number": "X"},
    }
    tracker.coordinator = coordinator
    tracker.api = MagicMock()
    tracker.api.connected.return_value = True
    return tracker


def _host(source="dhcp", **kwargs):
    return {
        "source": source,
        "address": "192.0.2.10",
        "interface": "ether2",
        "available": False,
        "last-seen": False,
        **kwargs,
    }


async def test_tracker_pings_in_one_job_and_keeps_state_on_error(
    hass: HomeAssistant,
) -> None:
    """P6 / item 10: one job pings due hosts; API errors keep the last state."""
    recent = utcnow() - timedelta(seconds=30)
    old = utcnow() - timedelta(seconds=150)
    hosts = {
        "m1": _host(),
        "m2": _host(available=True, **{"last-seen": old}),
        "m3": _host(available=True, **{"last-seen": recent}),
        "m4": _host("wireless", available=True),
        "m5": _host(address="unknown"),
    }
    tracker = _tracker(hass, hosts)
    tracker.api.arp_ping.side_effect = lambda address, interface: (
        interface == "ether2"
    )

    data = await tracker._async_update_data()

    assert data["host"] is hosts
    # m3 was seen recently, m4 is wireless, m5 has no address
    assert [c.args for c in tracker.api.arp_ping.call_args_list] == [
        ("192.0.2.10", "ether2"),
        ("192.0.2.10", "bridge1"),
    ]
    assert hosts["m1"]["available"] is True
    assert hosts["m1"]["last-seen"] > recent
    assert hosts["m2"]["available"] is False
    assert hosts["m2"]["last-seen"] == old

    # API error: the previous state is kept
    tracker.api.arp_ping.side_effect = None
    tracker.api.arp_ping.return_value = None
    hosts["m1"]["last-seen"] = old
    await tracker._async_update_data()
    assert hosts["m1"]["available"] is True
    assert hosts["m1"]["last-seen"] == old


async def test_tracker_without_test_policy_returns_hosts(hass: HomeAssistant) -> None:
    """M4: without the test policy, hosts are still returned, just not pinged."""
    hosts = {"m4": _host("wireless", available=True)}
    tracker = _tracker(hass, hosts, access=("read",))

    data = await tracker._async_update_data()

    assert data == {"host": hosts, "routerboard": {"serial-number": "X"}}
    tracker.api.arp_ping.assert_not_called()
    tracker.coordinator.async_process_host.assert_awaited_once()


# ---------------------------------------------------------------------------
# Review 2 M8 / M9
# ---------------------------------------------------------------------------
async def test_tracker_fails_while_the_router_poll_fails(hass: HomeAssistant) -> None:
    """M8: cached wireless rows must not be stamped as just seen forever."""
    import pytest
    from homeassistant.helpers.update_coordinator import UpdateFailed

    hosts = {"m4": _host("wireless", available=True)}
    tracker = _tracker(hass, hosts)
    tracker.coordinator.last_update_success = False

    with pytest.raises(UpdateFailed):
        await tracker._async_update_data()
    tracker.coordinator.async_process_host.assert_not_awaited()
    tracker.api.arp_ping.assert_not_called()

    tracker.coordinator.last_update_success = True
    await tracker._async_update_data()
    tracker.coordinator.async_process_host.assert_awaited_once()


def _host_coordinator(access, bridge_hosts):
    from custom_components.mikrotik_router.coordinator import MikrotikCoordinator

    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.support_capsman = False
    coordinator.support_wireless = False
    coordinator.host_hass_recovered = True
    coordinator.config_entry = MagicMock(options={})
    coordinator.async_mac_lookup = MagicMock(lookup=AsyncMock(return_value="Acme"))
    coordinator.ds = {
        "access": list(access),
        "capsman_hosts": {},
        "wireless_hosts": {},
        "dhcp": {
            mac: {
                "enabled": True,
                "address": f"192.0.2.{i}",
                "mac-address": mac,
                "interface": "bridge",
                "comment": "",
                "host-name": "unknown",
            }
            for i, mac in ((10, "m1"), (11, "m2"))
        },
        "arp": {},
        "host": {},
        "host_hass": {},
        "hostspot_host": {},
        "dns": {},
        "resource": {},
        "bridge_host": bridge_hosts,
    }
    return coordinator


async def test_wired_hosts_without_test_policy_use_the_bridge_table() -> None:
    """M9: a read-only account has no ping; bridge hosts are the evidence."""
    coordinator = _host_coordinator(("read",), {"m1": {"enabled": True}})
    await coordinator._async_process_host()

    hosts = coordinator.ds["host"]
    assert hosts["m1"]["available"] is True
    assert hosts["m1"]["last-seen"] is not False
    assert hosts["m1"]["presence-tracked"] is True
    # Leased but never seen on a port: unknown rather than permanently away
    assert hosts["m2"]["last-seen"] is False
    assert hosts["m2"]["presence-tracked"] is False


async def test_wired_hosts_with_test_policy_are_left_to_the_ping() -> None:
    coordinator = _host_coordinator(("read", "test"), {"m1": {"enabled": True}})
    await coordinator._async_process_host()

    hosts = coordinator.ds["host"]
    assert hosts["m1"]["last-seen"] is False
    assert hosts["m1"]["presence-tracked"] is True
    assert hosts["m2"]["presence-tracked"] is True


def test_untracked_host_is_unavailable() -> None:
    from custom_components.mikrotik_router.device_tracker import (
        MikrotikHostDeviceTracker,
    )
    from custom_components.mikrotik_router.entity import MikrotikEntity

    tracker = object.__new__(MikrotikHostDeviceTracker)
    with patch.object(MikrotikEntity, "available", True):
        tracker._data = {"presence-tracked": False}
        assert tracker.available is False
        tracker._data = {"presence-tracked": True}
        assert tracker.available is True
