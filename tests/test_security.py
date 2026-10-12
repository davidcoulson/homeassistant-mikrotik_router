"""Connection security defaults and repair issues (M1)."""

from unittest.mock import patch

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
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router import async_migrate_entry
from custom_components.mikrotik_router.const import (
    DEFAULT_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

from test_devices import SERIAL, _fake_tracker_update, _fake_update


def test_new_entries_default_to_verified_ssl():
    """M1: the config form starts with SSL and verification on."""
    assert DEFAULT_SSL is True
    assert DEFAULT_VERIFY_SSL is True


@pytest.mark.parametrize(
    ("use_ssl", "verify", "issue"),
    [
        (False, False, "insecure_connection"),
        (True, False, "unverified_connection"),
        (True, True, None),
    ],
)
async def test_insecure_entries_get_a_repair_issue(
    hass: HomeAssistant, use_ssl, verify, issue
) -> None:
    """M1: existing entries keep their settings but get a warning."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=3,
        title="Mikrotik",
        unique_id=f"routerboard:{SERIAL.lower()}",
        data={
            CONF_NAME: "Mikrotik",
            CONF_HOST: "192.0.2.1",
            CONF_PORT: 0,
            CONF_SSL: use_ssl,
            CONF_VERIFY_SSL: verify,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
        },
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

    issues = {
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN
    }
    expected = {f"{issue}_{entry.entry_id}"} if issue else set()
    assert issues == expected
    # Settings are not changed behind the user's back
    assert entry.data[CONF_SSL] is use_ssl
    assert entry.data[CONF_VERIFY_SSL] is verify

    await hass.config_entries.async_remove(entry.entry_id)
    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]


async def test_migrating_v1_entry_keeps_verification_off(hass: HomeAssistant) -> None:
    """M1: entries from before verify_ssl existed keep not verifying."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id="routerboard:abc",
        data={
            CONF_NAME: "Mikrotik",
            CONF_HOST: "192.0.2.1",
            CONF_PORT: 0,
            CONF_SSL: True,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.data[CONF_VERIFY_SSL] is False
