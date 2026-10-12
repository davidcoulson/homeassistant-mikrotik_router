"""Read-only mode: no entity or API call may change the router."""

import asyncio
import inspect
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router.const import CONF_READ_ONLY, DOMAIN
from custom_components.mikrotik_router.coordinator import MikrotikCoordinator
from custom_components.mikrotik_router.exceptions import ApiReadOnly
from custom_components.mikrotik_router.mikrotikapi import MikrotikAPI
from custom_components.mikrotik_router.switch import MikrotikPortSwitch
from custom_components.mikrotik_router.switch_types import SENSOR_TYPES as SWITCH_TYPES

SERIAL = "HEX123"
ETHER_MAC = "AA:BB:CC:DD:EE:01"


# ---------------------------------------------------------------------------
# API layer (the safety net)
# ---------------------------------------------------------------------------
def _api(read_only=True):
    api = MikrotikAPI("router.example", "test", "test", use_ssl=False, read_only=read_only)
    api._connection = MagicMock()
    api._connected = True
    return api


# Every MikrotikAPI method that can change the router, with arguments
WRITE_CALLS = {
    "set_value": ("/interface", "name", "ether1", "disabled", True),
    "execute": ("/system", "reboot", None, None),
    "run_script": ("backup",),
    "take_client_traffic_snapshot": (True,),
}

# Methods that only read, or manage the local socket
READ_METHODS = {
    "arp_ping",
    "close",
    "connect",
    "connected",
    "connection_check",
    "disconnect",
    "error_to_strings",
    "has_reconnected",
    "is_accounting_and_local_traffic_enabled",
    "query",
}


def test_every_api_method_is_classified() -> None:
    """A new MikrotikAPI method must be added to one of the lists above,
    so a new write path can't slip past the read-only guard."""
    public = {
        name
        for name, member in inspect.getmembers(MikrotikAPI, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == READ_METHODS | set(WRITE_CALLS)


@pytest.mark.parametrize("method", sorted(WRITE_CALLS))
def test_api_write_paths_refuse(method) -> None:
    """Write paths raise before they touch the connection."""
    api = _api()
    with pytest.raises(ApiReadOnly):
        getattr(api, method)(*WRITE_CALLS[method])
    api._connection.path.assert_not_called()
    assert not api._connection.mock_calls


@pytest.mark.parametrize(
    "command", ["reboot", "check-for-updates", "set", "remove", "add", "run", "reset-counters"]
)
def test_api_query_refuses_commands(command) -> None:
    """query() runs only monitor/print commands on a read-only connection."""
    api = _api()
    with pytest.raises(ApiReadOnly):
        api.query("/system", command=command)
    assert not api._connection.mock_calls


def test_api_query_allows_reads() -> None:
    """Plain reads and monitor commands still work."""
    api = _api()
    api._connection.path.return_value = [{"name": "ether1"}]
    assert api.query("/interface") == [{"name": "ether1"}]

    response = api._connection.path.return_value = MagicMock()
    response.return_value = iter([{"status": "link-ok"}])
    assert api.query(
        "/interface/ethernet", command="monitor", args={"numbers": "*1", "once": True}
    ) == [{"status": "link-ok"}]


def test_api_writes_still_work_without_option() -> None:
    """The guard is off by default, so existing entries behave as before."""
    api = _api(read_only=False)
    response = api._connection.path.return_value
    response.__iter__.return_value = iter([{".id": "*1", "name": "ether1"}])
    assert api.set_value("/interface", "name", "ether1", "disabled", True)
    response.update.assert_called_once_with(**{".id": "*1", "disabled": True})


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
def _coordinator(read_only):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.config_entry = MagicMock(options={CONF_READ_ONLY: read_only})
    coordinator.ds = {"access": ["write", "policy", "reboot"], "fw-update": {}}
    coordinator.api = _api(read_only)
    coordinator.api.query = MagicMock(return_value=[{"status": "System is already up to date"}])
    coordinator.api.execute = MagicMock(side_effect=ApiReadOnly("execute"))
    return coordinator


def test_firmware_check_does_not_execute_when_read_only() -> None:
    """The firmware check reads the last status instead of running a command."""
    coordinator = _coordinator(True)
    coordinator.get_firmware_update()
    coordinator.api.execute.assert_not_called()
    assert coordinator.ds["fw-update"]["status"] == "System is already up to date"


def test_firmware_check_executes_without_option() -> None:
    coordinator = _coordinator(False)
    coordinator.api.execute = MagicMock(return_value=True)
    coordinator.get_firmware_update()
    coordinator.api.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
async def test_entity_command_refused_before_api(hass: HomeAssistant) -> None:
    """Even a switch built by hand can't reach the API on a read-only entry."""
    description = next(d for d in SWITCH_TYPES if d.func == "MikrotikPortSwitch")
    coordinator = MagicMock()
    row = {
        "name": "ether1",
        "default-name": "ether1",
        "type": "ether",
        "about": "",
        "port-mac-address": ETHER_MAC,
        "enabled": True,
        "running": True,
        "comment": "",
    }
    coordinator.data = {"access": ["write"], "interface": {"ether1": row}}
    coordinator.config_entry.data = {"name": "Mikrotik"}
    coordinator.config_entry.options = {CONF_READ_ONLY: True}
    coordinator.command_lock = asyncio.Lock()
    switch = MikrotikPortSwitch(coordinator, description, "ether1")
    switch.hass = hass

    with pytest.raises(ServiceValidationError):
        await switch.async_turn_off()
    coordinator.set_value.assert_not_called()


def _interface(name, itype="ether"):
    return {
        "default-name": name,
        "name": name,
        "type": itype,
        "running": True,
        "enabled": True,
        "port-mac-address": ETHER_MAC,
        "comment": "",
        "rx": None,
        "tx": None,
        "client-ip-address": "",
        "client-mac-address": "",
    }


async def _fake_update(self):
    self.ds["access"] = ["write", "policy", "reboot", "test"]
    self.ds["routerboard"] = {
        "routerboard": True,
        "serial-number": SERIAL,
        "model": "CRS305",
        "current-firmware": "7.20",
        "upgrade-firmware": "7.20",
    }
    self.ds["resource"] = {
        "board-name": "CRS305",
        "platform": "MikroTik",
        "version": "7.20.1",
    }
    self.ds["fw-update"] = {
        "available": False,
        "status": "System is already up to date",
        "channel": "stable",
        "installed-version": "7.20.1",
        "latest-version": "7.20.1",
    }
    self.ds["interface"] = {"ether1": _interface("ether1")}
    self.ds["nat"] = {
        "*1": {"uniq-id": "*1", "name": "r1", "comment": "", "enabled": True, ".id": "*1"}
    }
    self.ds["script"] = {"backup": {"name": "backup", "last-started": "", "run-count": 0}}
    return self.ds


async def _fake_tracker_update(self):
    return None


async def _setup(hass, entry):
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


def _entry(read_only):
    return MockConfigEntry(
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
        options={
            CONF_READ_ONLY: read_only,
            "sensor_nat": True,
            "sensor_scripts": True,
        },
    )


async def test_read_only_entry_has_no_write_entities(hass: HomeAssistant) -> None:
    """No switch or button exists, stale ones are removed, state stays visible
    as binary sensors and updates can't be installed."""
    entry = _entry(True)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    # Left over from before the option was turned on
    stale_switch = ent_reg.async_get_or_create(
        "switch", DOMAIN, "mikrotik-interface-ether1", config_entry=entry
    )
    stale_button = ent_reg.async_get_or_create(
        "button", DOMAIN, "mikrotik-script-backup", config_entry=entry
    )

    await _setup(hass, entry)

    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    domains = {e.domain for e in entities}
    assert "switch" not in domains
    assert "button" not in domains
    assert ent_reg.async_get(stale_switch.entity_id) is None
    assert ent_reg.async_get(stale_button.entity_id) is None
    assert not hass.states.async_entity_ids("switch")
    assert not hass.states.async_entity_ids("button")

    unique_ids = {e.unique_id for e in entities if e.domain == "binary_sensor"}
    assert "mikrotik-interface_state-ether1" in unique_ids
    assert "mikrotik-nat_state-1" in unique_ids
    port_state = ent_reg.async_get_entity_id(
        "binary_sensor", DOMAIN, "mikrotik-interface_state-ether1"
    )
    assert hass.states.get(port_state).state == "on"

    for entity_id in hass.states.async_entity_ids("update"):
        features = hass.states.get(entity_id).attributes["supported_features"]
        assert not features & UpdateEntityFeature.INSTALL
        assert not features & UpdateEntityFeature.BACKUP

    # The coordinator and tracker APIs both refuse writes
    runtime = entry.runtime_data
    assert runtime.data_coordinator.api.read_only
    assert runtime.tracker_coordinator.api.read_only

    # Turning the option off brings the switches back and drops the stand-ins
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
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_READ_ONLY: False}
        )
        await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert "switch" in {e.domain for e in entities}
    assert "button" in {e.domain for e in entities}
    assert not [e for e in entities if "_state-" in e.unique_id]
    assert not entry.runtime_data.data_coordinator.api.read_only


async def test_existing_entries_default_to_writable(hass: HomeAssistant) -> None:
    """Entries without the option keep their switches."""
    entry = _entry(False)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={k: v for k, v in entry.options.items() if k != CONF_READ_ONLY}
    )
    await _setup(hass, entry)
    assert hass.states.async_entity_ids("switch")
    assert not entry.runtime_data.data_coordinator.api.read_only


# ---------------------------------------------------------------------------
# Config and options flow
# ---------------------------------------------------------------------------
async def test_options_flow_sets_read_only(hass: HomeAssistant) -> None:
    entry = _entry(False)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "basic_options"
    schema_keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_READ_ONLY in schema_keys

    with patch("custom_components.mikrotik_router.async_reload_entry"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_READ_ONLY: True}
        )
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options[CONF_READ_ONLY] is True


async def test_user_step_stores_read_only_as_option(hass: HomeAssistant) -> None:
    user_input = {
        CONF_NAME: "Edge",
        CONF_HOST: "192.0.2.1",
        CONF_USERNAME: "u",
        CONF_PASSWORD: "p",
        CONF_PORT: 0,
        CONF_SSL: True,
        CONF_VERIFY_SSL: True,
        CONF_READ_ONLY: True,
    }
    with (
        patch(
            "custom_components.mikrotik_router.config_flow.validate_input",
            return_value=("routerboard:abc", None),
        ),
        patch("custom_components.mikrotik_router.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert CONF_READ_ONLY not in result["data"]
    assert result["options"] == {CONF_READ_ONLY: True}
