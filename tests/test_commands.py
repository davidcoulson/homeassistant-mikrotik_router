"""Entity command serialisation and refresh (M10, L3, coordinator item 24)."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.mikrotik_router.switch import MikrotikPortSwitch
from custom_components.mikrotik_router.switch_types import SENSOR_TYPES


def _port_switch(hass, row):
    description = next(d for d in SENSOR_TYPES if d.func == "MikrotikPortSwitch")
    coordinator = MagicMock()
    coordinator.data = {
        "access": ["write"],
        description.data_path: {"ether1": row},
    }
    coordinator.config_entry.data = {"name": "Mikrotik"}
    coordinator.command_lock = asyncio.Lock()
    coordinator.async_request_full_refresh = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    switch = MikrotikPortSwitch(coordinator, description, "ether1")
    switch.hass = hass
    return switch, coordinator


def _row(**kwargs):
    return {
        "name": "ether1",
        "default-name": "ether1",
        "type": "ether",
        "about": "",
        "port-mac-address": "AA:BB:CC:DD:EE:01",
        "enabled": True,
        "running": True,
        "comment": "",
        **kwargs,
    }


async def test_port_and_poe_change_in_one_job(hass: HomeAssistant) -> None:
    """L3 / item 24: both changes share one job; the refresh is debounced."""
    switch, coordinator = _port_switch(hass, _row(**{"poe-out": "auto-on"}))
    threads = set()

    def set_value(*args):
        threads.add(threading.get_ident())
        return True

    coordinator.set_value.side_effect = set_value

    await switch.async_turn_off()

    assert [c.args[3:] for c in coordinator.set_value.call_args_list] == [
        ("disabled", True),
        ("poe-out", "off"),
    ]
    assert len(threads) == 1
    coordinator.async_request_full_refresh.assert_awaited_once()
    coordinator.async_refresh.assert_not_called()

    coordinator.set_value.reset_mock()
    switch._data["poe-out"] = "off"
    await switch.async_turn_on()
    assert [c.args[3:] for c in coordinator.set_value.call_args_list] == [
        ("disabled", False),
        ("poe-out", "auto-on"),
    ]


async def test_commands_run_one_at_a_time(hass: HomeAssistant) -> None:
    """M10: concurrent commands for one router don't pile up in the executor."""
    switch, coordinator = _port_switch(hass, _row())
    running = 0
    peak = 0
    lock = threading.Lock()

    def slow(*args):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return True

    await asyncio.gather(*(switch.async_run_routeros(slow) for _ in range(5)))
    assert peak == 1
