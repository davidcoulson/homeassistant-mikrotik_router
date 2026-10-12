"""P7, item 27: discovery builds each entity object once."""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.mikrotik_router.entity import async_add_entities
from custom_components.mikrotik_router.sensor_types import (
    MikrotikSensorEntityDescription,
)


async def test_discovery_builds_objects_once(hass: HomeAssistant) -> None:
    description = MikrotikSensorEntityDescription(
        key="queue",
        data_path="queue",
        data_attribute="rate",
        data_reference="name",
        func="Fake",
    )
    built = []

    class Fake:
        def __init__(self, coordinator, entity_description, uid):
            built.append(uid)
            self.unique_id = f"x-{uid}"

    coordinator = MagicMock()
    coordinator.data = {"queue": {"a": {"name": "a"}}}
    listeners = []
    coordinator.async_add_listener = lambda cb: listeners.append(cb)
    entry = MagicMock(options={})
    added = []

    await async_add_entities(
        hass, entry, added.extend, {"Fake": Fake}, (description,), [], coordinator
    )
    assert built == ["a"]
    coordinator.data["queue"]["b"] = {"name": "b"}
    listeners[0]()
    listeners[0]()
    assert built == ["a", "b"]
    assert [entity.unique_id for entity in added] == ["x-a", "x-b"]
