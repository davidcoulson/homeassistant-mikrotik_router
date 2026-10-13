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
