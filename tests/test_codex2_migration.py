"""Review 2 H1: the legacy read_only option migrates to allowed_changes."""

import pytest
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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router import async_migrate_entry
from custom_components.mikrotik_router.const import (
    ALL_CHANGES,
    CONF_ALLOWED_CHANGES,
    DOMAIN,
)
from custom_components.mikrotik_router.helper import allowed_changes


def _entry(options, data_extra=None, minor=3):
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=minor,
        unique_id="routerboard:abc",
        data={
            CONF_NAME: "Mikrotik",
            CONF_HOST: "192.0.2.1",
            CONF_PORT: 0,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
            **(data_extra or {}),
        },
        options=options,
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"read_only": True}, []),
        ({"read_only": "yes"}, []),
        ({"read_only": False}, list(ALL_CHANGES)),
        # An existing granular choice wins
        ({"read_only": True, CONF_ALLOWED_CHANGES: ["firmware"]}, ["firmware"]),
        ({"read_only": False, CONF_ALLOWED_CHANGES: []}, []),
    ],
)
async def test_read_only_option_migrates(hass: HomeAssistant, options, expected) -> None:
    entry = _entry(options)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert "read_only" not in entry.options
    assert entry.options[CONF_ALLOWED_CHANGES] == expected
    assert entry.minor_version == 4
    assert allowed_changes(entry.options) == frozenset(expected)


async def test_read_only_in_data_migrates(hass: HomeAssistant) -> None:
    entry = _entry({}, {"read_only": True})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert "read_only" not in entry.data
    assert entry.options[CONF_ALLOWED_CHANGES] == []


async def test_entry_without_legacy_option_is_unchanged(hass: HomeAssistant) -> None:
    """No read_only option: nothing is invented (all changes stay allowed)."""
    entry = _entry({"sensor_nat": True})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert CONF_ALLOWED_CHANGES not in entry.options
    assert entry.minor_version == 4
