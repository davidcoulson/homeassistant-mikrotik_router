"""Support for the Mikrotik Router switches."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import MikrotikConfigEntry
from .entity import MikrotikEntity, async_add_entities
from .helper import format_attribute
from .switch_types import (
    SENSOR_TYPES,
    SENSOR_SERVICES,
    DEVICE_ATTRIBUTES_IFACE_ETHER,
    DEVICE_ATTRIBUTES_IFACE_SFP,
    DEVICE_ATTRIBUTES_IFACE_WIRELESS,
)


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    add_entities_callback: AddEntitiesCallback,
) -> None:
    """Set up entry for component"""
    dispatcher = {
        "MikrotikSwitch": MikrotikSwitch,
        "MikrotikPortSwitch": MikrotikPortSwitch,
        "MikrotikNATSwitch": MikrotikNATSwitch,
        "MikrotikMangleSwitch": MikrotikMangleSwitch,
        "MikrotikFilterSwitch": MikrotikFilterSwitch,
        "MikrotikQueueSwitch": MikrotikQueueSwitch,
        "MikrotikKidcontrolPauseSwitch": MikrotikKidcontrolPauseSwitch,
    }
    await async_add_entities(
        hass,
        config_entry,
        add_entities_callback,
        dispatcher,
        SENSOR_TYPES,
        SENSOR_SERVICES,
    )


# ---------------------------
#   MikrotikSwitch
# ---------------------------
class MikrotikSwitch(MikrotikEntity, SwitchEntity, RestoreEntity):
    """Representation of a switch."""

    @property
    def is_on(self) -> bool:
        """Return true if device is on."""
        return self._data[self.entity_description.data_attribute]

    @property
    def icon(self) -> str:
        """Return the icon."""
        if self._data[self.entity_description.data_attribute]:
            return self.entity_description.icon_enabled
        else:
            return self.entity_description.icon_disabled

    def turn_on(self, **kwargs: Any) -> None:
        """Required abstract method."""
        pass

    def turn_off(self, **kwargs: Any) -> None:
        """Required abstract method."""
        pass

    async def async_turn_on(self) -> None:
        """Turn on the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        value = self._data[self.entity_description.data_reference]
        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, False
        )
        await self.coordinator.async_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        value = self._data[self.entity_description.data_reference]
        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, True
        )
        await self.coordinator.async_refresh()


# ---------------------------
#   MikrotikPortSwitch
# ---------------------------
class MikrotikPortSwitch(MikrotikSwitch):
    """Representation of a network port switch."""

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        attributes = super().extra_state_attributes

        if self._data["type"] == "ether":
            for variable in DEVICE_ATTRIBUTES_IFACE_ETHER:
                if variable in self._data:
                    attributes[format_attribute(variable)] = self._data[variable]

            if "sfp-shutdown-temperature" in self._data:
                for variable in DEVICE_ATTRIBUTES_IFACE_SFP:
                    if variable in self._data:
                        attributes[format_attribute(variable)] = self._data[variable]

        elif self._data["type"] == "wlan":
            for variable in DEVICE_ATTRIBUTES_IFACE_WIRELESS:
                if variable in self._data:
                    attributes[format_attribute(variable)] = self._data[variable]

        return attributes

    @property
    def icon(self) -> str:
        """Return the icon."""
        if self._data["running"]:
            icon = self.entity_description.icon_enabled
        else:
            icon = self.entity_description.icon_disabled

        if not self._data["enabled"]:
            icon = "mdi:lan-disconnect"

        return icon

    async def async_turn_on(self) -> None:
        """Turn on the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        if self._data["about"] == "managed by CAPsMAN":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="managed_by_capsman",
                translation_placeholders={"entity": str(self._data[param])},
            )
        if "-" in self._data["port-mac-address"]:
            param = "name"
        value = self._data[self.entity_description.data_reference]
        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, False
        )

        if "poe-out" in self._data and self._data["poe-out"] == "off":
            path = "/interface/ethernet"
            await self.async_run_routeros(
                self.coordinator.set_value,
                path,
                param,
                value,
                "poe-out",
                "auto-on",
            )

        await self.coordinator.async_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        if self._data["about"] == "managed by CAPsMAN":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="managed_by_capsman",
                translation_placeholders={"entity": str(self._data[param])},
            )
        if "-" in self._data["port-mac-address"]:
            param = "name"
        value = self._data[self.entity_description.data_reference]
        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, True
        )

        if "poe-out" in self._data and self._data["poe-out"] == "auto-on":
            path = "/interface/ethernet"
            await self.async_run_routeros(
                self.coordinator.set_value, path, param, value, "poe-out", "off"
            )

        await self.coordinator.async_refresh()


# NAT, Mangle and Filter rules are keyed and switched by their RouterOS .id,
# which the generic switch already does.
MikrotikNATSwitch = MikrotikSwitch
MikrotikMangleSwitch = MikrotikSwitch
MikrotikFilterSwitch = MikrotikSwitch


# ---------------------------
#   MikrotikQueueSwitch
# ---------------------------
class MikrotikQueueSwitch(MikrotikSwitch):
    """Representation of a queue switch."""

    async def async_turn_on(self) -> None:
        """Turn on the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = ".id"
        value = None
        for uid in self.coordinator.data["queue"]:
            if self.coordinator.data["queue"][uid]["name"] == f"{self._data['name']}":
                value = self.coordinator.data["queue"][uid][".id"]

        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, False
        )
        await self.coordinator.async_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = ".id"
        value = None
        for uid in self.coordinator.data["queue"]:
            if self.coordinator.data["queue"][uid]["name"] == f"{self._data['name']}":
                value = self.coordinator.data["queue"][uid][".id"]

        mod_param = self.entity_description.data_switch_parameter
        await self.async_run_routeros(
            self.coordinator.set_value, path, param, value, mod_param, True
        )
        await self.coordinator.async_refresh()


# ---------------------------
#   MikrotikKidcontrolPauseSwitch
# ---------------------------
class MikrotikKidcontrolPauseSwitch(MikrotikSwitch):
    """Representation of a queue switch."""

    async def async_turn_on(self) -> None:
        """Turn on the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        value = self._data[self.entity_description.data_reference]
        command = "resume"
        await self.async_run_routeros(
            self.coordinator.execute, path, command, param, value
        )
        await self.coordinator.async_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the switch."""
        self.require_access("write")

        path = self.entity_description.data_switch_path
        param = self.entity_description.data_reference
        value = self._data[self.entity_description.data_reference]
        command = "pause"
        await self.async_run_routeros(
            self.coordinator.execute, path, command, param, value
        )
        await self.coordinator.async_refresh()
