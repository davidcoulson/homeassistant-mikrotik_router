"""Review permission boundaries with legacy entries and a fake transport."""

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from librouteros.api import Path

from custom_components.mikrotik_router import async_migrate_entry
from custom_components.mikrotik_router.const import ALL_CHANGES, CONF_ALLOWED_CHANGES
from custom_components.mikrotik_router.exceptions import ApiReadOnly
from custom_components.mikrotik_router.helper import allowed_changes
from custom_components.mikrotik_router.mikrotikapi import MikrotikAPI

from test_codex2_migration import _entry


@pytest.mark.parametrize(
    ("options", "data_extra", "expected"),
    [
        ({"read_only": True}, {}, []),
        ({"read_only": False}, {}, list(ALL_CHANGES)),
        ({"read_only": "yes"}, {}, []),
        ({"read_only": None}, {}, []),
        ({"read_only": True, CONF_ALLOWED_CHANGES: ["firmware"]}, {}, ["firmware"]),
        ({CONF_ALLOWED_CHANGES: None}, {}, []),
        ({}, {"read_only": True}, []),
        ({"read_only": True}, {"read_only": False}, []),
        ({"read_only": False}, {"read_only": True}, list(ALL_CHANGES)),
    ],
)
async def test_permissions_before_migration(
    hass: HomeAssistant, options, data_extra, expected
) -> None:
    """Options can be edited before a disabled legacy entry is migrated."""
    entry = _entry(options, data_extra)
    entry.add_to_hass(hass)
    assert allowed_changes({**entry.data, **entry.options}) == frozenset(expected)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Submitting an unchanged form must preserve the effective permissions.
    defaults = result["data_schema"]({})
    assert defaults[CONF_ALLOWED_CHANGES] == expected
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], defaults
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert allowed_changes(entry.options) == frozenset(expected)
    assert entry.minor_version == 3


@pytest.mark.parametrize(
    ("options", "data_extra"),
    [
        ({"read_only": None}, {}),
        ({}, {"read_only": None}),
        ({"read_only": True}, {"read_only": False}),
    ],
)
async def test_migration_keeps_legacy_restriction(
    hass: HomeAssistant, options, data_extra
) -> None:
    entry = _entry(options, data_extra)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.options[CONF_ALLOWED_CHANGES] == []
    assert "read_only" not in entry.options
    assert "read_only" not in entry.data


def _api(allowed):
    api = MikrotikAPI("router.example", "u", "p", allowed_changes=allowed)
    transport = MagicMock(return_value=iter([]))
    api._connection = MagicMock()
    api._connection.path.side_effect = lambda path: Path(path, transport)
    api._connected = True
    return api, transport


@pytest.mark.parametrize("method", ["execute", "query"])
@pytest.mark.parametrize("command", ["/interface/set", "../interface/set"])
def test_escaping_commands_never_reach_transport(method, command) -> None:
    """Exercise the same librouteros Path that previously replaced the base."""
    api, transport = _api(["firmware"])
    with pytest.raises(ApiReadOnly):
        if method == "execute":
            api.execute("/system", command, None, None, {"disabled": True})
        else:
            api.query("/system", command=command, args={"disabled": True})
    transport.assert_not_called()
    api._connection.path.assert_not_called()


@pytest.mark.parametrize("command", [None, "print"])
def test_query_materializes_real_path(command) -> None:
    api, transport = _api([])
    rows = [{".id": "*1", "disabled": False}]
    transport.return_value = iter(rows)
    result = api.query("/interface", command=command)
    assert isinstance(result, list)
    assert result == rows
    assert not hasattr(result, "update")
    transport.assert_called_once_with("/interface/print")


def test_relative_snapshot_command_reaches_transport() -> None:
    api, transport = _api(["accounting"])
    assert api.execute("/ip/accounting", "snapshot/take", None, None)
    transport.assert_called_once_with("/ip/accounting/snapshot/take")
