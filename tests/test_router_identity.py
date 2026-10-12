"""Router device identity for CHR/x86 and hidden serials (H1)."""

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
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_router.const import DOMAIN

from test_devices import _fake_tracker_update, _fake_update


async def _chr_update(self):
    await _fake_update(self)
    self.ds["routerboard"]["serial-number"] = "N/A"
    self.ds["routerboard"]["routerboard"] = False
    self.ds["resource"]["board-name"] = "CHR"
    return self.ds


def _entry(hass, name, host):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=3,
        title=name,
        unique_id=f"chr:{host}",
        data={
            CONF_NAME: name,
            CONF_HOST: host,
            CONF_PORT: 0,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry, update=_chr_update):
    with (
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikCoordinator._async_update_data",
            update,
        ),
        patch(
            "custom_components.mikrotik_router.coordinator.MikrotikTrackerCoordinator._async_update_data",
            _fake_tracker_update,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _router(dev_reg, entry):
    (router,) = [
        d
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if (DOMAIN, entry.entry_id) in d.identifiers
    ]
    return router


async def test_two_chr_routers_get_separate_devices(hass: HomeAssistant) -> None:
    """Two CHRs no longer register (DOMAIN, "N/A") router devices."""
    entry_a = _entry(hass, "Alpha", "192.0.2.1")
    entry_b = _entry(hass, "Beta", "192.0.2.2")
    dev_reg = dr.async_get(hass)
    for entry in (entry_a, entry_b):
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, "N/A")},
            connections={(DOMAIN, "N/A")},
            name=f"{entry.title} CHR",
        )
        dev_reg.async_config_entry_unloaded(entry.entry_id)

    # Setting up the integration loads both entries
    await _setup(hass, entry_a)
    assert entry_b.runtime_data

    router_a = _router(dev_reg, entry_a)
    router_b = _router(dev_reg, entry_b)
    assert router_a.id != router_b.id
    ent_reg = er.async_get(hass)
    for entry, router in ((entry_a, router_a), (entry_b, router_b)):
        children = dr.async_entries_for_parent_device(dev_reg, router.id)
        assert {c.name for c in children} == {"ether1", "vlan10", "NAT"}
        assert any(
            e.device_id == router.id
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        )
    assert dev_reg.async_get_devices(identifiers={(DOMAIN, "N/A")}) == []


async def test_single_chr_device_is_rekeyed_in_place(hass: HomeAssistant) -> None:
    """An unshared "N/A" device keeps its ID and area."""
    entry = _entry(hass, "Alpha", "192.0.2.1")
    dev_reg = dr.async_get(hass)
    area = ar.async_get(hass).async_get_or_create("Rack")
    legacy = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "N/A")},
        connections={(DOMAIN, "N/A")},
        name="Alpha CHR",
    )
    dev_reg.async_update_device(legacy.id, area_id=area.id)
    dev_reg.async_config_entry_unloaded(entry.entry_id)

    await _setup(hass, entry)

    router = _router(dev_reg, entry)
    assert router.id == legacy.id
    assert router.area_id == area.id
    assert router.identifiers == {(DOMAIN, entry.entry_id)}
    assert (DOMAIN, "N/A") not in router.connections


async def test_unreadable_serial_keeps_existing_router_device(
    hass: HomeAssistant,
) -> None:
    """A RouterBOARD whose serial read fails once keeps its serial device."""
    entry = _entry(hass, "Alpha", "192.0.2.1")
    dev_reg = dr.async_get(hass)
    legacy = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "HEX123")},
        connections={(DOMAIN, "HEX123")},
        name="Alpha RB5009",
    )
    dev_reg.async_config_entry_unloaded(entry.entry_id)

    async def _unknown_serial(self):
        await _fake_update(self)
        self.ds["routerboard"]["serial-number"] = "unknown"
        return self.ds

    await _setup(hass, entry, _unknown_serial)

    assert dev_reg.async_get(legacy.id) is not None
    assert entry.runtime_data.router_device_id == legacy.id
    assert dev_reg.async_get_devices(identifiers={(DOMAIN, entry.entry_id)}) == []
