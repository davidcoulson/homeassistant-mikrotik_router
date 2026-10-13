"""Allowed router changes: no entity or API call may make a change the
option forbids."""

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

from custom_components.mikrotik_router.const import (
    ALL_CHANGES,
    CHANGE_FIRMWARE,
    CHANGE_PORTS,
    CONF_ALLOWED_CHANGES,
    DOMAIN,
    WRITE_PATH_CHANGES,
)
from custom_components.mikrotik_router.coordinator import MikrotikCoordinator
from custom_components.mikrotik_router.exceptions import ApiReadOnly
from custom_components.mikrotik_router.helper import allowed_changes
from custom_components.mikrotik_router.mikrotikapi import MikrotikAPI
from custom_components.mikrotik_router.switch import MikrotikPortSwitch
from custom_components.mikrotik_router.switch_types import SENSOR_TYPES as SWITCH_TYPES

SERIAL = "HEX123"
ETHER_MAC = "AA:BB:CC:DD:EE:01"
# What the user wants on a switch: firmware updates, but no port toggles
ALL_BUT_PORTS = [change for change in ALL_CHANGES if change != CHANGE_PORTS]


# ---------------------------------------------------------------------------
# API layer (the safety net)
# ---------------------------------------------------------------------------
def _api(allowed=()):
    api = MikrotikAPI(
        "router.example", "test", "test", use_ssl=False, allowed_changes=allowed
    )
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


def test_api_writes_still_work_with_every_change_allowed() -> None:
    """All changes are allowed by default, so existing entries behave as before."""
    api = _api(ALL_CHANGES)
    response = api._connection.path.return_value
    response.__iter__.return_value = iter([{".id": "*1", "name": "ether1"}])
    assert api.set_value("/interface", "name", "ether1", "disabled", True)
    response.update.assert_called_once_with(**{".id": "*1", "disabled": True})


def test_api_refuses_only_disallowed_paths() -> None:
    """Without ports, port writes are refused but firmware commands run."""
    api = _api(ALL_BUT_PORTS)
    for path in ("/interface", "/interface/ethernet"):
        with pytest.raises(ApiReadOnly):
            api.set_value(path, "name", "ether1", "disabled", True)
    assert not api._connection.mock_calls

    api._connection.path.return_value.return_value = iter([])
    assert api.execute("/system/package/update", "install", None, None)
    assert api._connection.path.call_args.args == ("/system/package/update",)


def test_api_refuses_unlisted_paths_when_restricted() -> None:
    """A path with no kind of change is refused unless everything is allowed."""
    api = _api(ALL_BUT_PORTS)
    with pytest.raises(ApiReadOnly):
        api.execute("/ip/dns/cache", "flush", None, None)
    assert not api._connection.mock_calls


def test_every_write_path_has_a_change() -> None:
    """Every switch writes to a path that belongs to a kind of change."""
    for description in SWITCH_TYPES:
        assert WRITE_PATH_CHANGES[description.data_switch_path] in ALL_CHANGES
    assert set(WRITE_PATH_CHANGES.values()) == set(ALL_CHANGES)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, set(ALL_CHANGES)),
        ({CONF_ALLOWED_CHANGES: []}, set()),
        ({CONF_ALLOWED_CHANGES: ["firmware", "bogus"]}, {"firmware"}),
        # Only an absent option means "everything"; a malformed one fails closed
        ({CONF_ALLOWED_CHANGES: "firmware"}, set()),
        ({CONF_ALLOWED_CHANGES: None}, set()),
        ({CONF_ALLOWED_CHANGES: 1}, set()),
        ({CONF_ALLOWED_CHANGES: {"ports": True}}, set()),
        ({CONF_ALLOWED_CHANGES: ["firmware", None, ["x"]]}, {"firmware"}),
    ],
)
def test_allowed_changes_option(options, expected) -> None:
    assert allowed_changes(options) == expected


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
def _coordinator(allowed):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.config_entry = MagicMock(options={CONF_ALLOWED_CHANGES: allowed})
    coordinator.ds = {"access": ["write", "policy", "reboot"], "fw-update": {}}
    coordinator.api = _api(allowed)
    coordinator.api.query = MagicMock(return_value=[{"status": "System is already up to date"}])
    coordinator.api.execute = MagicMock(side_effect=ApiReadOnly("execute"))
    return coordinator


def test_firmware_check_does_not_execute_without_firmware_changes() -> None:
    """The firmware check reads the last status instead of running a command."""
    coordinator = _coordinator([])
    coordinator.get_firmware_update()
    coordinator.api.execute.assert_not_called()
    assert coordinator.ds["fw-update"]["status"] == "System is already up to date"


def test_firmware_check_executes_with_firmware_changes() -> None:
    coordinator = _coordinator([CHANGE_FIRMWARE])
    coordinator.api.execute = MagicMock(return_value=True)
    coordinator.get_firmware_update()
    coordinator.api.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
async def test_entity_command_refused_by_api(hass: HomeAssistant) -> None:
    """Even a port switch built by hand can't toggle the port without ports."""
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
    coordinator.config_entry.options = {CONF_ALLOWED_CHANGES: ALL_BUT_PORTS}
    coordinator.command_lock = asyncio.Lock()
    api = _api(ALL_BUT_PORTS)
    coordinator.set_value = api.set_value
    switch = MikrotikPortSwitch(coordinator, description, "ether1")
    switch.hass = hass

    with pytest.raises(ServiceValidationError) as error:
        await switch.async_turn_off()
    assert error.value.translation_key == "change_not_allowed"
    assert not api._connection.mock_calls


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


def _entry(allowed=None):
    options = {"sensor_nat": True, "sensor_scripts": True}
    if allowed is not None:
        options[CONF_ALLOWED_CHANGES] = allowed
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
        options=options,
    )


def _update_features(hass):
    return [
        hass.states.get(entity_id).attributes["supported_features"]
        for entity_id in hass.states.async_entity_ids("update")
    ]


async def test_read_only_entry_has_no_write_entities(hass: HomeAssistant) -> None:
    """With nothing allowed, no switch or button exists, stale ones are
    removed, state stays visible as binary sensors and updates can't be
    installed."""
    entry = _entry([])
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    # Left over from before the option was changed
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

    features = _update_features(hass)
    assert features
    for feature in features:
        assert not feature & UpdateEntityFeature.INSTALL
        assert not feature & UpdateEntityFeature.BACKUP

    # The coordinator and tracker APIs both refuse writes
    runtime = entry.runtime_data
    assert runtime.data_coordinator.api.allowed_changes == frozenset()
    assert runtime.tracker_coordinator.api.allowed_changes == frozenset()

    # Allowing everything brings the switches back and drops the stand-ins
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
            entry, options={**entry.options, CONF_ALLOWED_CHANGES: list(ALL_CHANGES)}
        )
        await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert "switch" in {e.domain for e in entities}
    assert "button" in {e.domain for e in entities}
    assert not [e for e in entities if "_state-" in e.unique_id]
    assert entry.runtime_data.data_coordinator.api.allowed_changes == set(ALL_CHANGES)


async def test_updates_allowed_ports_not(hass: HomeAssistant) -> None:
    """Without ports: no port switch (its state shows as a binary sensor),
    other switches and buttons stay, and updates can be installed."""
    entry = _entry(ALL_BUT_PORTS)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    stale_port = ent_reg.async_get_or_create(
        "switch", DOMAIN, "mikrotik-interface-ether1", config_entry=entry
    )
    await _setup(hass, entry)

    assert ent_reg.async_get(stale_port.entity_id) is None
    unique_ids = {
        (e.domain, e.unique_id)
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    assert ("switch", "mikrotik-interface-ether1") not in unique_ids
    assert ("binary_sensor", "mikrotik-interface_state-ether1") in unique_ids
    assert ("switch", "mikrotik-nat-1") in unique_ids
    assert ("binary_sensor", "mikrotik-nat_state-1") not in unique_ids
    assert hass.states.async_entity_ids("button")

    features = _update_features(hass)
    assert features
    for feature in features:
        assert feature & UpdateEntityFeature.INSTALL

    api = entry.runtime_data.data_coordinator.api
    with pytest.raises(ApiReadOnly):
        api.set_value("/interface", "name", "ether1", "disabled", True)


async def test_existing_entries_allow_every_change(hass: HomeAssistant) -> None:
    """Entries without the option keep their switches."""
    entry = _entry()
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    assert hass.states.async_entity_ids("switch")
    assert entry.runtime_data.data_coordinator.api.allowed_changes == set(ALL_CHANGES)


# ---------------------------------------------------------------------------
# Config and options flow
# ---------------------------------------------------------------------------
async def test_options_flow_sets_allowed_changes(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "basic_options"
    schema_keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_ALLOWED_CHANGES in schema_keys

    with patch("custom_components.mikrotik_router.async_reload_entry"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ALLOWED_CHANGES: ALL_BUT_PORTS}
        )
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options[CONF_ALLOWED_CHANGES] == ALL_BUT_PORTS


async def test_user_step_stores_allowed_changes_as_option(hass: HomeAssistant) -> None:
    user_input = {
        CONF_NAME: "Edge",
        CONF_HOST: "192.0.2.1",
        CONF_USERNAME: "u",
        CONF_PASSWORD: "p",
        CONF_PORT: 0,
        CONF_SSL: True,
        CONF_VERIFY_SSL: True,
        CONF_ALLOWED_CHANGES: [],
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
    assert CONF_ALLOWED_CHANGES not in result["data"]
    assert result["options"] == {CONF_ALLOWED_CHANGES: []}


# ---------------------------------------------------------------------------
# Review 2 M2: query() never returns a writable librouteros path
# ---------------------------------------------------------------------------
def test_query_has_no_raw_path_option() -> None:
    """query(return_list=False) used to hand out a Path that can write."""
    api = _api()
    with pytest.raises(TypeError):
        api.query("/interface", return_list=False)
    assert not api._connection.mock_calls


def test_query_returns_data_not_a_path() -> None:
    api = _api()
    api._connection.path.return_value = [{"name": "ether1"}]
    result = api.query("/interface")
    assert result == [{"name": "ether1"}]
    assert isinstance(result, list)
    # A list has no update(), so it can't write
    assert not hasattr(result, "update")


def test_query_positional_arguments_cannot_select_a_raw_path() -> None:
    """The old fourth positional argument was return_list."""
    api = _api()
    api._connection.path.return_value = [{"name": "ether1"}]
    assert api.query("/interface", None, None, False) == [{"name": "ether1"}]
