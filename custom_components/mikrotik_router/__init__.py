"""Mikrotik Router integration."""

from __future__ import annotations

import voluptuous as vol
import logging

from homeassistant.components import zone
from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.device_tracker.const import CONF_ASSOCIATED_ZONE
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SSL,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_VERIFY_SSL,
    CONF_ZONE,
    STATE_HOME,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.util import slugify

from .const import (
    PLATFORMS,
    DOMAIN,
    ALL_CHANGES,
    CONF_ALLOWED_CHANGES,
    LEGACY_READ_ONLY,
    LEGACY_VERIFY_SSL,
    CHANGE_SCRIPTS,
)
from .coordinator import (
    MikrotikConfigEntry,
    MikrotikData,
    MikrotikCoordinator,
    MikrotikTrackerCoordinator,
)
from .helper import (
    INVALID_ROUTER_IDENTIFIERS,
    allowed_changes,
    child_device_identifier,
    router_unique_id,
)
from .binary_sensor_types import (
    SWITCH_STATE_CHANGES,
    SENSOR_TYPES as BINARY_SENSOR_TYPES,
)
from .button_types import SENSOR_TYPES as BUTTON_TYPES
from .sensor_types import SENSOR_TYPES as SENSOR_PLATFORM_TYPES
from .switch_types import SENSOR_TYPES as SWITCH_TYPES, switch_change
from .binary_sensor_types import SENSOR_SERVICES as BINARY_SENSOR_SERVICES
from .button_types import SENSOR_SERVICES as BUTTON_SERVICES
from .device_tracker_types import SENSOR_SERVICES as DEVICE_TRACKER_SERVICES
from .sensor_types import SENSOR_SERVICES as SENSOR_PLATFORM_SERVICES
from .switch_types import SENSOR_SERVICES as SWITCH_SERVICES
from .update_types import SENSOR_SERVICES as UPDATE_SERVICES

SCRIPT_SCHEMA = vol.Schema(
    {vol.Required("router"): cv.string, vol.Required("script"): cv.string}
)

ENTITY_SERVICES = {
    Platform.BINARY_SENSOR: BINARY_SENSOR_SERVICES,
    Platform.BUTTON: BUTTON_SERVICES,
    Platform.DEVICE_TRACKER: DEVICE_TRACKER_SERVICES,
    Platform.SENSOR: SENSOR_PLATFORM_SERVICES,
    Platform.SWITCH: SWITCH_SERVICES,
    Platform.UPDATE: UPDATE_SERVICES,
}

STATIC_CHILD_GROUPS = {
    description.ha_group
    for description in (
        *BINARY_SENSOR_TYPES,
        *BUTTON_TYPES,
        *SENSOR_PLATFORM_TYPES,
        *SWITCH_TYPES,
    )
    if description.ha_group
    and description.ha_group != "System"
    and not description.ha_group.startswith("data__")
}

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   async_setup
# ---------------------------
async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the Mikrotik Router integration."""
    registered_services = set()
    for entity_domain, entity_services in ENTITY_SERVICES.items():
        for service_name, schema, func in entity_services:
            if service_name in registered_services:
                continue

            service.async_register_platform_entity_service(
                hass,
                DOMAIN,
                service_name,
                entity_domain=entity_domain,
                schema=schema,
                func=func,
            )
            registered_services.add(service_name)

    return True


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> bool:
    """Set up a config entry."""
    _async_check_connection_security(hass, config_entry)
    coordinator = MikrotikCoordinator(hass, config_entry)
    apis = [coordinator.api]

    async def _async_close_apis() -> None:
        """Close the RouterOS sockets on unload or failed setup."""
        for api in apis:
            await hass.async_add_executor_job(api.close)

    config_entry.async_on_unload(_async_close_apis)
    await coordinator.async_config_entry_first_refresh()
    _async_update_router_unique_id(hass, config_entry, coordinator)
    _async_migrate_firewall_unique_ids(hass, config_entry, coordinator)
    router_device_id, router_identifier = _async_setup_router_device(
        hass, config_entry, coordinator
    )
    _async_migrate_child_devices(hass, config_entry, coordinator, router_device_id)
    _async_remove_disallowed_changes(hass, config_entry)
    coordinatorTracker = MikrotikTrackerCoordinator(hass, config_entry, coordinator)
    apis.append(coordinatorTracker.api)
    await coordinatorTracker.async_config_entry_first_refresh()
    config_entry.runtime_data = MikrotikData(
        data_coordinator=coordinator,
        tracker_coordinator=coordinatorTracker,
        router_device_id=router_device_id,
        router_identifier=router_identifier,
    )

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    _async_remove_orphaned_devices(hass, config_entry, router_device_id)

    config_entry.async_on_unload(config_entry.add_update_listener(async_reload_entry))

    return True


# ---------------------------
#   _async_check_connection_security
# ---------------------------
@callback
def _async_check_connection_security(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> None:
    """Raise a repair issue for entries without SSL or certificate checks.

    Existing entries keep their settings; the user decides when to switch.
    """
    issue_ids = {
        "insecure_connection": f"insecure_connection_{config_entry.entry_id}",
        "unverified_connection": f"unverified_connection_{config_entry.entry_id}",
    }
    if not config_entry.data.get(CONF_SSL):
        active = "insecure_connection"
    elif not config_entry.data.get(CONF_VERIFY_SSL):
        active = "unverified_connection"
    else:
        active = None

    for translation_key, issue_id in issue_ids.items():
        if translation_key != active:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue

        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={"name": config_entry.title},
        )


# ---------------------------
#   _async_migrate_firewall_unique_ids
# ---------------------------
FIREWALL_RULE_PATHS = ("nat", "mangle", "filter")


@callback
def _async_migrate_firewall_unique_ids(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    coordinator: MikrotikCoordinator,
) -> None:
    """Move NAT/Mangle/Filter switches from content-based to .id-based unique ids.

    The old unique id was built from the rule's matchers, so editing a rule
    created a second entity. Runs on every setup and only renames entities whose
    rule is present in the current data, so it is idempotent.
    """
    ent_reg = entity_registry.async_get(hass)
    inst = config_entry.data[CONF_NAME].lower()
    for key in FIREWALL_RULE_PATHS:
        rows = (coordinator.data or {}).get(key) or {}
        old_to_new = {}
        duplicates = set()
        for row in rows.values():
            if not row.get("uniq-id") or not row.get(".id"):
                continue
            old = f"{inst}-{key}-{slugify(str(row['uniq-id']).lower())}"
            if old in old_to_new:
                duplicates.add(old)
            old_to_new[old] = f"{inst}-{key}-{slugify(str(row['.id']).lower())}"

        for old, new in old_to_new.items():
            if old in duplicates:
                # Duplicate rules had no entity: nothing to migrate
                continue
            entity_id = ent_reg.async_get_entity_id(Platform.SWITCH, DOMAIN, old)
            if entity_id is None:
                continue
            if ent_reg.async_get_entity_id(Platform.SWITCH, DOMAIN, new):
                _LOGGER.debug("Not migrating %s: %s already exists", entity_id, new)
                continue
            _LOGGER.debug("Migrating %s unique id %s -> %s", entity_id, old, new)
            ent_reg.async_update_entity(entity_id, new_unique_id=new)


# ---------------------------
#   _async_router_identifier
# ---------------------------
@callback
def _async_router_identifier(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    coordinator: MikrotikCoordinator,
) -> tuple[str, str]:
    """Return the router device identifier, migrating placeholder serials.

    RouterBOARDs keep their serial number as identifier. CHR/x86 report the
    serial as "N/A" and some boards hide it, so such routers registered a
    (DOMAIN, "N/A") or (DOMAIN, "unknown") device, shared between entries on
    older HA releases. They now use the config entry ID.
    """
    serial_number = f"{coordinator.data['routerboard']['serial-number']}"
    if serial_number.strip().casefold() not in INVALID_ROUTER_IDENTIFIERS:
        _async_rekey_placeholder_router_device(
            hass, config_entry, (DOMAIN, serial_number)
        )
        return (DOMAIN, serial_number)

    fallback = (DOMAIN, config_entry.entry_id)
    registry = device_registry.async_get(hass)
    for device in device_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        router_ids = {
            value
            for domain, value in device.identifiers
            if domain == DOMAIN and not value.startswith(f"{config_entry.entry_id}-")
        }
        if not router_ids or fallback[1] in router_ids:
            continue

        if any(
            value.strip().casefold() not in INVALID_ROUTER_IDENTIFIERS
            for value in router_ids
        ):
            # A real serial is registered, but this poll couldn't read it:
            # keep using the existing device.
            return (DOMAIN, sorted(router_ids)[0])

        # Re-key the placeholder device in place, so its ID, area, name and
        # children stay the same.
        registry.async_update_device(
            device.id,
            new_identifiers={fallback},
            new_connections={
                connection
                for connection in device.connections
                if connection[0] != DOMAIN
            },
        )
        break

    return fallback


# ---------------------------
#   _async_rekey_placeholder_router_device
# ---------------------------
def _async_rekey_placeholder_router_device(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    identifier: tuple[str, str],
) -> None:
    """Re-key a placeholder router device to a serial that is now readable.

    A board whose first poll had no valid serial registered its router device
    under the config entry ID (or "N/A"/"unknown"). Create the real device
    in place instead of beside it, so the child devices keep their parent.
    """
    registry = device_registry.async_get(hass)
    if registry.async_get_device_by_identifier(identifier, config_entry.entry_id):
        return

    placeholders = {config_entry.entry_id} | set(INVALID_ROUTER_IDENTIFIERS)
    for device in device_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        router_ids = {
            value
            for domain, value in device.identifiers
            if domain == DOMAIN and not value.startswith(f"{config_entry.entry_id}-")
        }
        if not router_ids or not all(
            value == config_entry.entry_id or value.strip().casefold() in placeholders
            for value in router_ids
        ):
            continue

        registry.async_update_device(
            device.id,
            new_identifiers={
                (domain, value)
                for domain, value in device.identifiers
                if not (domain == DOMAIN and value in router_ids)
            }
            | {identifier},
        )
        return


# ---------------------------
#   _async_setup_router_device
# ---------------------------
def _async_setup_router_device(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    coordinator: MikrotikCoordinator,
) -> tuple[str, tuple[str, str]]:
    """Set up the router device before its child devices."""
    registry = device_registry.async_get(hass)
    identifier = _async_router_identifier(hass, config_entry, coordinator)
    protocol = "https" if config_entry.data[CONF_SSL] else "http"
    device = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={identifier},
        name=f"{config_entry.data[CONF_NAME]} {coordinator.data['resource']['board-name']}",
        model=f"{coordinator.data['resource']['board-name']}",
        manufacturer=f"{coordinator.data['resource']['platform']}",
        sw_version=f"{coordinator.data['resource']['version']}",
        configuration_url=f"{protocol}://{config_entry.data[CONF_HOST]}",
    )
    device_id = device.id

    @callback
    def _async_update_sw_version() -> None:
        """Keep the router firmware version current after RouterOS upgrades."""
        version = (coordinator.data or {}).get("resource", {}).get("version")
        if not version:
            return
        router = registry.async_get(device_id)
        if router is not None and router.sw_version != f"{version}":
            registry.async_update_device(device_id, sw_version=f"{version}")

    config_entry.async_on_unload(coordinator.async_add_listener(_async_update_sw_version))
    return device_id, identifier


# ---------------------------
#   _async_migrate_child_devices
# ---------------------------
def _async_migrate_child_devices(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    coordinator: MikrotikCoordinator,
    router_device_id: str,
) -> None:
    """Tag legacy per-group devices so they convert to child devices in place.

    Older versions registered interfaces and rule groups as standalone devices
    keyed only by connections. Adding the child identifiers lets the device
    registry convert them on entity setup, keeping their ids, areas and names.
    """
    entry_id = config_entry.entry_id
    by_connection: dict[tuple[str, str], tuple[str, str]] = {
        (DOMAIN, group): child_device_identifier(entry_id, group)
        for group in STATIC_CHILD_GROUPS
    }
    for interface in coordinator.data.get("interface", {}).values():
        if not interface.get("port-mac-address") or not interface.get("default-name"):
            continue
        by_connection[
            (
                device_registry.CONNECTION_NETWORK_MAC,
                device_registry.format_mac(interface["port-mac-address"]),
            )
        ] = child_device_identifier(entry_id, "interface", interface["default-name"])

    registry = device_registry.async_get(hass)
    for device in device_registry.async_entries_for_config_entry(registry, entry_id):
        if device.id == router_device_id:
            continue
        identifiers = {
            by_connection[connection]
            for connection in device.connections
            if connection in by_connection
        }
        if len(identifiers) == 1 and not identifiers <= device.identifiers:
            registry.async_update_device(
                device.id, new_identifiers=device.identifiers | identifiers
            )


# Marks registry entries this integration disabled because of the allowed
# changes option, so only those are enabled again (integration-disabled
# entities, which are off by default, stay off).
DISABLED_BY_ALLOWED_CHANGES = entity_registry.RegistryEntryDisabler.CONFIG_ENTRY


# ---------------------------
#   _async_remove_disallowed_changes
# ---------------------------
@callback
def _async_remove_disallowed_changes(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> None:
    """Disable registry entries that don't fit the allowed changes option.

    Switches for a kind of change that isn't allowed are disabled, and so are
    buttons without script changes, so nothing active in the registry can make
    a change the option forbids. The state binary sensors that stand in for
    switches are disabled once their kind of change is allowed again. They are
    disabled, not removed, so entity IDs, names and history survive switching
    a category off and on; only entries this integration disabled are
    enabled again. Empty devices are removed afterwards by
    _async_remove_orphaned_devices.
    """
    allowed = allowed_changes(config_entry.options)
    prefix = f"{config_entry.data[CONF_NAME].lower()}-"
    switch_prefixes = {
        f"{prefix}{description.key}-": switch_change(description)
        for description in SWITCH_TYPES
    }
    state_prefixes = {
        f"{prefix}{key}-": change for key, change in SWITCH_STATE_CHANGES.items()
    }
    ent_reg = entity_registry.async_get(hass)
    for entry in entity_registry.async_entries_for_config_entry(
        ent_reg, config_entry.entry_id
    ):
        unique_id = str(entry.unique_id)
        if entry.domain == Platform.BUTTON:
            # None: not managed; True: must be disabled; False: may be enabled
            disable = CHANGE_SCRIPTS not in allowed
        elif entry.domain == Platform.SWITCH:
            disable = next(
                (
                    change not in allowed
                    for switch_prefix, change in switch_prefixes.items()
                    if unique_id.startswith(switch_prefix)
                ),
                None,
            )
        elif entry.domain == Platform.BINARY_SENSOR:
            disable = next(
                (
                    change in allowed
                    for state_prefix, change in state_prefixes.items()
                    if unique_id.startswith(state_prefix)
                ),
                None,
            )
        else:
            disable = None

        if disable is None:
            continue
        if disable and entry.disabled_by is None:
            _LOGGER.debug("Disabling %s (change not allowed)", entry.entity_id)
            ent_reg.async_update_entity(
                entry.entity_id, disabled_by=DISABLED_BY_ALLOWED_CHANGES
            )
        elif not disable and entry.disabled_by == DISABLED_BY_ALLOWED_CHANGES:
            _LOGGER.debug("Enabling %s (change allowed again)", entry.entity_id)
            ent_reg.async_update_entity(entry.entity_id, disabled_by=None)


# ---------------------------
#   _async_remove_orphaned_devices
# ---------------------------
def _async_remove_orphaned_devices(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry, router_device_id: str
) -> None:
    """Remove devices of this entry that no longer have any entities.

    Runs after the platforms are set up, so every entity has been linked to its
    current device. Legacy group devices that could not be converted in place
    (and child devices of deleted interfaces or rule groups) are left empty.
    """
    dev_reg = device_registry.async_get(hass)
    ent_reg = entity_registry.async_get(hass)
    used_device_ids = {
        entry.device_id
        for entry in entity_registry.async_entries_for_config_entry(
            ent_reg, config_entry.entry_id
        )
        if entry.device_id
    }
    devices = (
        *device_registry.async_entries_for_config_entry(dev_reg, config_entry.entry_id),
        *device_registry.async_child_entries_for_config_entry(
            dev_reg, config_entry.entry_id
        ),
    )
    removable = {
        device.id: device
        for device in devices
        if device.id != router_device_id and device.id not in used_device_ids
    }
    # A device that is the parent of a device we keep must stay too
    changed = True
    while changed:
        changed = False
        for device in devices:
            if device.id in removable:
                continue
            parent_id = getattr(device, "parent_device_id", None) or getattr(
                device, "via_device_id", None
            )
            if parent_id in removable:
                del removable[parent_id]
                changed = True
    for device in removable.values():
        _LOGGER.debug("Removing orphaned device %s (%s)", device.name, device.id)
        dev_reg.async_remove_device(device.id)


# ---------------------------
#   _async_update_router_unique_id
# ---------------------------
def _async_update_router_unique_id(
    hass: HomeAssistant,
    config_entry: MikrotikConfigEntry,
    coordinator: MikrotikCoordinator,
) -> None:
    """Upgrade a fallback config-entry ID to a RouterBOARD serial."""
    if config_entry.unique_id and not config_entry.unique_id.startswith("endpoint:"):
        return

    unique_id = router_unique_id(
        config_entry.data[CONF_HOST],
        config_entry.data[CONF_PORT],
        config_entry.data[CONF_SSL],
        coordinator.ds["routerboard"].get("serial-number"),
    )
    if unique_id == config_entry.unique_id:
        return

    existing_entry = hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, unique_id
    )
    if existing_entry and existing_entry.entry_id != config_entry.entry_id:
        _LOGGER.warning(
            "Mikrotik %s has the same RouterBOARD identifier as configuration %s; preserving both existing entries",
            config_entry.title,
            existing_entry.title,
        )
        return

    hass.config_entries.async_update_entry(config_entry, unique_id=unique_id)


# ---------------------------
#   async_reload_entry
# ---------------------------
async def async_reload_entry(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> None:
    """Reload the config entry when it changed."""
    await hass.config_entries.async_reload(config_entry.entry_id)


# ---------------------------
#   async_unload_entry
# ---------------------------
async def async_unload_entry(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)


# ---------------------------
#   async_remove_entry
# ---------------------------
async def async_remove_entry(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> None:
    """Drop the entry's repair issues when it is deleted."""
    for prefix in ("insecure_connection", "unverified_connection"):
        ir.async_delete_issue(hass, DOMAIN, f"{prefix}_{config_entry.entry_id}")


# ---------------------------
#   async_remove_config_entry_device
# ---------------------------
async def async_remove_config_entry_device(
    hass, config_entry: MikrotikConfigEntry, device_entry: device_registry.DeviceEntry
) -> bool:
    """Allow removing stale devices only.

    The router and any device with a live entity would be recreated on the
    next reload, losing the user's area and name, so they can't be removed.
    """
    runtime_data = getattr(config_entry, "runtime_data", None)
    if runtime_data is not None and device_entry.id == runtime_data.router_device_id:
        return False

    ent_reg = entity_registry.async_get(hass)
    for entry in entity_registry.async_entries_for_device(
        ent_reg, device_entry.id, include_disabled_entities=True
    ):
        if entry.config_entry_id != config_entry.entry_id:
            continue
        state = hass.states.get(entry.entity_id)
        if state is not None and state.state != STATE_UNAVAILABLE:
            return False

    return True


# ---------------------------
#   async_migrate_entry
# ---------------------------
def _legacy_zone_entity_id(hass: HomeAssistant, legacy_zone) -> str:
    """Convert the legacy integration-wide zone to a zone entity ID."""
    zone_name = str(legacy_zone or STATE_HOME).strip()
    normalized_zone_name = zone_name.casefold()

    if normalized_zone_name in (STATE_HOME, zone.ENTITY_ID_HOME):
        return zone.ENTITY_ID_HOME

    if normalized_zone_name.startswith(f"{zone.DOMAIN}."):
        return normalized_zone_name

    for zone_state in hass.states.async_all(zone.DOMAIN):
        if zone_state.name.casefold() == normalized_zone_name:
            return zone_state.entity_id

    return f"{zone.DOMAIN}.{slugify(zone_name)}"


def _async_migrate_device_tracker_zones(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry, legacy_zone
) -> None:
    """Move the integration-wide zone to per-entity tracker options."""
    registry = entity_registry.async_get(hass)
    associated_zone = _legacy_zone_entity_id(hass, legacy_zone)

    for entry in entity_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        if entry.domain != DEVICE_TRACKER_DOMAIN or entry.platform != DOMAIN:
            continue

        tracker_options = dict(entry.options.get(DEVICE_TRACKER_DOMAIN, {}))
        if CONF_ASSOCIATED_ZONE in tracker_options:
            continue

        tracker_options[CONF_ASSOCIATED_ZONE] = associated_zone
        registry.async_update_entity_options(
            entry.entity_id, DEVICE_TRACKER_DOMAIN, tracker_options
        )


def _async_migrate_temperature_sensor_units(
    hass: HomeAssistant, config_entry: MikrotikConfigEntry
) -> None:
    """Remove the integration-provided temperature display unit."""
    registry = entity_registry.async_get(hass)

    for entry in entity_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        if (
            entry.domain != SENSOR_DOMAIN
            or entry.platform != DOMAIN
            or entry.original_device_class != SensorDeviceClass.TEMPERATURE
        ):
            continue

        if f"{SENSOR_DOMAIN}.private" in entry.options:
            registry.async_update_entity_options(
                entry.entity_id, f"{SENSOR_DOMAIN}.private", None
            )

        sensor_options = entry.options.get(SENSOR_DOMAIN, {})
        if CONF_UNIT_OF_MEASUREMENT not in sensor_options:
            registry.async_update_entity(entry.entity_id, unit_of_measurement=None)


async def async_migrate_entry(hass: HomeAssistant, config_entry: MikrotikConfigEntry):
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    new_data = {**config_entry.data}
    new_options = {**config_entry.options}
    new_version = config_entry.version
    new_minor_version = config_entry.minor_version
    new_unique_id = config_entry.unique_id

    if new_version < 2:
        new_data[CONF_VERIFY_SSL] = LEGACY_VERIFY_SSL
        new_version = 2
        new_minor_version = 1

    if new_version == 2 and new_minor_version < 2:
        if new_unique_id is None:
            new_unique_id = router_unique_id(
                new_data[CONF_HOST],
                new_data[CONF_PORT],
                new_data[CONF_SSL],
            )
        new_minor_version = 2

    if new_version < 3:
        legacy_zone = new_options.get(CONF_ZONE, STATE_HOME)
        _async_migrate_device_tracker_zones(hass, config_entry, legacy_zone)
        new_options.pop(CONF_ZONE, None)
        new_version = 3
        new_minor_version = 1

    if new_version == 3 and new_minor_version < 2:
        new_minor_version = 2

    if new_version == 3 and new_minor_version < 3:
        _async_migrate_temperature_sensor_units(hass, config_entry)
        new_minor_version = 3

    if new_version == 3 and new_minor_version < 4:
        # The removed "read_only" option must keep meaning "no changes".
        legacy_read_only = new_options.pop(LEGACY_READ_ONLY, None)
        legacy_read_only = new_data.pop(LEGACY_READ_ONLY, legacy_read_only)
        if CONF_ALLOWED_CHANGES not in new_options and legacy_read_only is not None:
            # Anything but an explicit false is treated as read-only
            new_options[CONF_ALLOWED_CHANGES] = (
                list(ALL_CHANGES) if legacy_read_only is False else []
            )
        new_minor_version = 4

    if (
        new_data != config_entry.data
        or new_options != config_entry.options
        or new_version != config_entry.version
        or new_minor_version != config_entry.minor_version
        or new_unique_id != config_entry.unique_id
    ):
        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            options=new_options,
            unique_id=new_unique_id,
            version=new_version,
            minor_version=new_minor_version,
        )

    _LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )
    return True
