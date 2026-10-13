"""Mikrotik sensor platform."""

from __future__ import annotations

from logging import getLogger
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MikrotikConfigEntry, MikrotikCoordinator
from .entity import MikrotikEntity, async_add_entities
from .helper import format_attribute, format_data_rate, is_sfp_port
from .sensor_types import (
    SENSOR_TYPES,
    SENSOR_SERVICES,
    DEVICE_ATTRIBUTES_IFACE_ETHER,
    DEVICE_ATTRIBUTES_IFACE_SFP,
    DEVICE_ATTRIBUTES_IFACE_WIRELESS,
)

_LOGGER = getLogger(__name__)


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
        "MikrotikSensor": MikrotikSensor,
        "MikrotikInterfaceTrafficSensor": MikrotikInterfaceTrafficSensor,
        "MikrotikInterfaceStatSensor": MikrotikInterfaceStatSensor,
        "MikrotikClientTrafficSensor": MikrotikClientTrafficSensor,
        "MikrotikDiagnosticSensor": MikrotikDiagnosticSensor,
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
#   MikrotikSensor
# ---------------------------
class MikrotikSensor(MikrotikEntity, SensorEntity):
    """Define an Mikrotik sensor."""

    def __init__(
        self,
        coordinator: MikrotikCoordinator,
        entity_description,
        uid: str | None = None,
    ):
        super().__init__(coordinator, entity_description, uid)
        self._attr_suggested_unit_of_measurement = (
            self.entity_description.suggested_unit_of_measurement
        )

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        """Return the value reported by the sensor."""
        return self._data[self.entity_description.data_attribute]

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the value is expressed in."""
        if self.entity_description.native_unit_of_measurement:
            if self.entity_description.native_unit_of_measurement.startswith("data__"):
                uom = self.entity_description.native_unit_of_measurement[6:]
                if uom in self._data:
                    return self._data[uom]

            return self.entity_description.native_unit_of_measurement

        return None


# ---------------------------
#   MikrotikInterfaceTrafficSensor
# ---------------------------
class MikrotikInterfaceTrafficSensor(MikrotikSensor):
    """Define an Mikrotik MikrotikInterfaceTrafficSensor sensor."""

    # Changes with every traffic window; the state already holds the value
    _unrecorded_attributes = frozenset({"formatted"})

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        attributes = super().extra_state_attributes

        # HA can't auto-scale one entity's unit; show a scaled value too
        if self.entity_description.device_class == SensorDeviceClass.DATA_RATE:
            attributes["formatted"] = format_data_rate(self.native_value)

        if self._data["type"] == "ether":
            for variable in DEVICE_ATTRIBUTES_IFACE_ETHER:
                if variable in self._data:
                    attributes[format_attribute(variable)] = self._data[variable]

            if is_sfp_port(self._data):
                for variable in DEVICE_ATTRIBUTES_IFACE_SFP:
                    if variable in self._data:
                        attributes[format_attribute(variable)] = self._data[variable]

        elif self._data["type"] == "wlan":
            for variable in DEVICE_ATTRIBUTES_IFACE_WIRELESS:
                if variable in self._data:
                    attributes[format_attribute(variable)] = self._data[variable]

        return attributes


# ---------------------------
#   MikrotikInterfaceStatSensor
# ---------------------------
class MikrotikInterfaceStatSensor(MikrotikSensor):
    """An interface counter or link reading; unknown until RouterOS reports it."""

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value reported by the sensor."""
        return self._data.get(self.entity_description.data_attribute)


# ---------------------------
#   MikrotikDiagnosticSensor
# ---------------------------
_LEADING_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


class MikrotikDiagnosticSensor(MikrotikSensor):
    """A numeric reading RouterOS reports as text, like "-65@6Mbps"."""

    @property
    def native_value(self) -> float | None:
        """Return the leading number of the value, or None."""
        value = self._data.get(self.entity_description.data_attribute)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and (match := _LEADING_NUMBER.match(value)):
            return float(match.group(1))
        return None


# ---------------------------
#   MikrotikClientTrafficSensor
# ---------------------------
class MikrotikClientTrafficSensor(MikrotikSensor):
    """Define an Mikrotik MikrotikClientTrafficSensor sensor."""

    @property
    def custom_name(self) -> str:
        """Return the name for this entity"""
        return f"{self.entity_description.name}"
