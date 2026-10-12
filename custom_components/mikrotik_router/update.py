"""Support for the Mikrotik Router update service."""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.components.update import (
    UpdateEntity,
    UpdateDeviceClass,
    UpdateEntityFeature,
)

from .coordinator import MikrotikConfigEntry, MikrotikCoordinator
from .entity import MikrotikEntity, async_add_entities, is_read_only
from .helper import routeros_release_name
from .update_types import (
    SENSOR_TYPES,
    SENSOR_SERVICES,
)
from packaging.version import Version

_LOGGER = getLogger(__name__)
DEVICE_UPDATE = "device_update"


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
        "MikrotikRouterOSUpdate": MikrotikRouterOSUpdate,
        "MikrotikRouterBoardFWUpdate": MikrotikRouterBoardFWUpdate,
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
#   MikrotikRouterOSUpdate
# ---------------------------
class MikrotikRouterOSUpdate(MikrotikEntity, UpdateEntity):
    """Define an Mikrotik Controller Update entity."""

    def __init__(
        self,
        coordinator: MikrotikCoordinator,
        entity_description,
        uid: str | None = None,
    ):
        """Set up device update entity."""
        super().__init__(coordinator, entity_description, uid)

        # Read-only entries show the update but can't install it
        self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
        if not is_read_only(coordinator.config_entry):
            self._attr_supported_features |= UpdateEntityFeature.INSTALL
            self._attr_supported_features |= UpdateEntityFeature.BACKUP
        self._attr_title = self.entity_description.title

    @property
    def is_on(self) -> bool:
        """Return true if device is on."""
        return self._data[self.entity_description.data_attribute]

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        version = self._data.get("installed-version")
        if not version or version == "unknown":
            version = self.coordinator.data.get("resource", {}).get("version")

        return version if version and version != "unknown" else None

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        version = self._data.get("latest-version")
        return version if version and version != "unknown" else None

    async def options_updated(self) -> None:
        """No action needed."""

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install an update."""
        self.require_access("write", "policy", "reboot")
        if backup:
            await self.async_run_routeros(
                self.coordinator.execute, "/system/backup", "save", None, None
            )

        await self.async_run_routeros(
            self.coordinator.execute,
            "/system/package/update",
            "install",
            None,
            None,
        )

    async def async_release_notes(self) -> str | None:
        """Return the release notes."""
        installed_version = self.installed_version
        latest_version = self.latest_version
        if installed_version is None or latest_version is None:
            return None

        try:
            session = async_get_clientsession(self.hass)
            changelogs = await fetch_changelogs(
                session, installed_version, latest_version
            )
            return "\n\n".join(changelogs)

        except Exception as e:
            _LOGGER.warning("Failed to download release notes (%s)", e)

        return "Error fetching release notes."

    @property
    def release_url(self) -> str:
        """URL to the full release notes of the latest version available."""
        return "https://mikrotik.com/download/changelogs"


# ---------------------------
#   MikrotikRouterBoardFWUpdate
# ---------------------------
class MikrotikRouterBoardFWUpdate(MikrotikEntity, UpdateEntity):
    """Define an Mikrotik Controller Update entity."""

    TYPE = DEVICE_UPDATE
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(
        self,
        coordinator: MikrotikCoordinator,
        entity_description,
        uid: str | None = None,
    ):
        """Set up device update entity."""
        super().__init__(coordinator, entity_description, uid)

        self._attr_supported_features = (
            UpdateEntityFeature(0)
            if is_read_only(coordinator.config_entry)
            else UpdateEntityFeature.INSTALL
        )
        self._attr_title = self.entity_description.title

    @property
    def is_on(self) -> bool:
        """Return true if device is on."""
        return self.installed_version != self.latest_version

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        return self._data.get("current-firmware")

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        return self._data.get("upgrade-firmware")

    async def options_updated(self) -> None:
        """No action needed."""

    async def async_install(self, version: str, backup: bool, **kwargs: Any) -> None:
        """Install an update."""
        self.require_access("write", "policy", "reboot")
        await self.async_run_routeros(
            self.coordinator.execute, "/system/routerboard", "upgrade", None, None
        )
        await self.async_run_routeros(
            self.coordinator.execute, "/system", "reboot", None, None
        )


MAX_PATCHES_PER_MINOR = 30


async def fetch_changelog(session, version: str) -> str | None:
    """Fetch the changelog for a version, or None when it does not exist."""
    url = f"https://cdn.mikrotik.com/routeros/{version}/CHANGELOG"
    try:
        async with session.get(url) as response:
            if response.status == 200:
                text = await response.text()
                return text.replace("*) ", "- ")
            if response.status == 404:
                return None
    except Exception:  # noqa: BLE001
        pass
    return ""


def _routeros_version(major: int, minor: int, patch: int) -> str:
    """Return a RouterOS release name; x.y.0 is published as x.y."""
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"


async def _fetch_minor_changelogs(
    session, major: int, minor: int, first_patch: int, last_patch: int | None
) -> list[str]:
    """Fetch the changelogs of one minor series, newest first.

    With an unknown last patch, probe patches in order until one is missing.
    """
    changelogs = []
    patch = first_patch
    while patch <= (MAX_PATCHES_PER_MINOR if last_patch is None else last_patch):
        changelog = await fetch_changelog(
            session, _routeros_version(major, minor, patch)
        )
        if changelog is None:
            if last_patch is None:
                break
        elif changelog:
            changelogs.append(changelog)
        patch += 1
    return changelogs[::-1]


async def fetch_changelogs(session, installed_version: str, latest_version: str) -> list:
    """Fetch the changelogs newer than installed_version up to latest_version.

    Returns them newest first. Minor series are fetched concurrently; patches
    within a series are probed sequentially so only existing releases are
    requested.
    """
    installed, installed_pre = routeros_release_name(installed_version)
    latest, latest_pre = routeros_release_name(latest_version)
    if latest_pre:
        # A prerelease changelog covers the whole rc/beta series
        if (latest, latest_pre) == (installed, installed_pre):
            return []
        changelog = await fetch_changelog(session, f"{latest}{latest_pre}")
        return [changelog] if changelog else []

    start = Version(installed)
    end = Version(latest)
    # From 7.20rc2 to 7.20, the 7.20 changelog itself is new
    include_start = bool(installed_pre)
    if end < start or (end == start and not include_start):
        return []
    if end.major != start.major:
        changelog = await fetch_changelog(
            session, _routeros_version(end.major, end.minor, end.micro)
        )
        return [changelog] if changelog else []

    series = []
    for minor in range(start.minor, end.minor + 1):
        if minor != start.minor:
            first_patch = 0
        else:
            first_patch = start.micro if include_start else start.micro + 1
        last_patch = end.micro if minor == end.minor else None
        series.append(
            _fetch_minor_changelogs(session, end.major, minor, first_patch, last_patch)
        )

    results = await asyncio.gather(*series)
    return [changelog for minor_logs in reversed(results) for changelog in minor_logs]
