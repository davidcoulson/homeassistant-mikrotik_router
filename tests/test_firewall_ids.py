"""Firewall rule switches are keyed on the RouterOS .id (M5, M6)."""

from unittest.mock import patch

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
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router.const import DOMAIN

from test_devices import SERIAL, _fake_tracker_update, _fake_update


def _rule(rule_id, uniq_id, name):
    return {
        ".id": rule_id,
        "uniq-id": uniq_id,
        "name": name,
        "comment": "",
        "enabled": True,
    }


async def test_firewall_unique_ids_migrate_to_rule_id(hass: HomeAssistant) -> None:
    """Old content-based unique ids move to .id, keeping the entity_id."""
    rules = {
        "*1": _rule("*1", "dstnat,dst-nat,tcp,any:443-any:192.0.2.5:any", "tcp:443"),
        # Two identical rules: they had no entity before, now each gets one
        "*2": _rule("*2", "dstnat,dst-nat,udp,any:53-any::any", "udp:53"),
        "*3": _rule("*3", "dstnat,dst-nat,udp,any:53-any::any", "udp:53"),
    }

    async def _update(self):
        await _fake_update(self)
        self.ds["nat"] = {uid: dict(row) for uid, row in rules.items()}
        return self.ds

    entry = MockConfigEntry(
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
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        "mikrotik-nat-dstnat_dst_nat_tcp_any_443_any_192_0_2_5_any",
        config_entry=entry,
        suggested_object_id="https_forward",
    )

    async def _setup():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with (
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikCoordinator._async_update_data",
            _update,
        ),
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikTrackerCoordinator._async_update_data",
            _fake_tracker_update,
        ),
    ):
        await _check(hass, entry, ent_reg, old, rules, _setup)


async def _check(hass, entry, ent_reg, old, rules, _setup):
    await _setup()

    migrated = ent_reg.async_get(old.entity_id)
    assert migrated.unique_id == "mikrotik-nat-1"
    nat_ids = sorted(
        e.unique_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.domain == "switch" and "-nat-" in e.unique_id
    )
    assert nat_ids == ["mikrotik-nat-1", "mikrotik-nat-2", "mikrotik-nat-3"]

    # Editing the rule changes uniq-id but not the entity (M5)
    rules["*1"]["uniq-id"] = "dstnat,dst-nat,tcp,any:8443-any:192.0.2.5:any"
    coordinator = entry.runtime_data.data_coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(
        [
            e
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
            if e.domain == "switch" and "-nat-" in e.unique_id
        ]
    ) == 3
    assert hass.states.get(old.entity_id).state == "on"

    # Reload is idempotent
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup()
    assert ent_reg.async_get(old.entity_id).unique_id == "mikrotik-nat-1"
