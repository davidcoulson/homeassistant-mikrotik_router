"""Device registry layout tests."""

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

SERIAL = "HEX123"
ETHER_MAC = "AA:BB:CC:DD:EE:01"
VLAN_PORT_MAC = f"{ETHER_MAC}-vlan10"


def _interface(name, default_name, port_mac, itype):
    return {
        "default-name": default_name,
        "name": name,
        "type": itype,
        "running": True,
        "enabled": True,
        "port-mac-address": port_mac,
        "mac-address": ETHER_MAC,
        "comment": "",
        "rx": 0,
        "tx": 0,
        "rx-current": 0,
        "tx-current": 0,
        "rx-total": 0,
        "tx-total": 0,
        "rx-attr": "",
        "tx-attr": "",
        "client-ip-address": "",
        "client-mac-address": "",
    }


async def _fake_update(self):
    self.ds["access"] = ["write", "policy", "reboot", "test"]
    self.ds["routerboard"] = {
        "routerboard": True,
        "serial-number": SERIAL,
        "model": "RB5009",
        "current-firmware": "7.20",
        "upgrade-firmware": "7.20",
    }
    self.ds["resource"] = {
        "board-name": "RB5009",
        "platform": "MikroTik",
        "version": "7.20.1",
    }
    self.ds["interface"] = {
        "ether1": _interface("ether1", "ether1", ETHER_MAC, "ether"),
        "vlan10": _interface("vlan10", "vlan10", VLAN_PORT_MAC, "vlan"),
    }
    self.ds["nat"] = {
        "r1": {
            "uniq-id": "r1",
            "name": "r1",
            "comment": "",
            "enabled": True,
            ".id": "*1",
        }
    }
    return self.ds


async def _fake_tracker_update(self):
    return None


async def test_child_devices_and_migration(hass: HomeAssistant, caplog) -> None:
    """Interfaces and rule groups become child devices, keeping legacy device ids."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=3,
        unique_id=f"routerboard:{SERIAL.lower()}",
        data={
            CONF_NAME: "Mikrotik",
            CONF_HOST: "192.0.2.1",
            CONF_PORT: 0,
            CONF_SSL: False,
            CONF_VERIFY_SSL: False,
            CONF_USERNAME: "u",
            CONF_PASSWORD: "p",
        },
        options={"sensor_port_traffic": True},
    )
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    area = ar.async_get(hass).async_get_or_create("Rack")
    router = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(DOMAIN, SERIAL)},
        identifiers={(DOMAIN, SERIAL)},
        name="Mikrotik RB5009",
    )
    legacy_ether = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, ETHER_MAC)},
        name="Mikrotik ether1",
        via_device_id=router.id,
    )
    dev_reg.async_update_device(legacy_ether.id, area_id=area.id)
    legacy_nat = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(DOMAIN, "NAT")},
        name="Mikrotik NAT",
        via_device_id=router.id,
    )
    # Simulate a restart: legacy devices come from a previous run, so not live
    dev_reg.async_config_entry_unloaded(entry.entry_id)

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

    children = {
        child.name: child
        for child in dr.async_entries_for_parent_device(dev_reg, router.id)
    }
    assert set(children) == {"ether1", "vlan10", "NAT"}
    assert children["ether1"].id == legacy_ether.id
    assert children["ether1"].area_id == area.id
    assert children["NAT"].id == legacy_nat.id

    # Only the router remains a main device
    assert [d.id for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)] == [
        router.id
    ]

    ent_reg = er.async_get(hass)
    device_ids = {
        e.device_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    assert device_ids <= {router.id, *(c.id for c in children.values())}
    assert children["vlan10"].id in device_ids

    # Router firmware version is kept in sync
    assert dev_reg.async_get(router.id).sw_version == "7.20.1"

    # Reload is idempotent
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
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert len(dr.async_entries_for_parent_device(dev_reg, router.id)) == 3
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")
        and "has not been tested" not in r.getMessage()
    ]
