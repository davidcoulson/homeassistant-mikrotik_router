"""MAC vendor lookup with a back-off when the OUI list is unavailable."""

from __future__ import annotations

import asyncio
from logging import getLogger
import os
from time import monotonic

from mac_vendor_lookup import AsyncMacLookup, BaseMacLookup

_LOGGER = getLogger(__name__)

LOAD_TIMEOUT = 120
RETRY_INTERVAL = 60 * 60


class VendorLookup:
    """Look up MAC vendors without retrying a failed download on every host.

    mac_vendor_lookup downloads the ~6 MB IEEE list when no cache exists.
    If that fails it leaves the prefixes empty, so every new host started
    another download; a truncated cache made every load raise. Here a
    failed load waits RETRY_INTERVAL, and a broken cache file is removed so
    the next attempt downloads a fresh one.
    """

    def __init__(self, hass) -> None:
        self._hass = hass
        self._lookup = AsyncMacLookup()
        self._retry_at = 0.0

    async def _async_load(self) -> bool:
        """Load the vendor list; return False while backing off."""
        if self._lookup.prefixes:
            return True
        if monotonic() < self._retry_at:
            return False

        try:
            async with asyncio.timeout(LOAD_TIMEOUT):
                await self._lookup.load_vendors()
        except Exception as err:
            _LOGGER.debug("Unable to load the MAC vendor list: %s", err)
            self._lookup.prefixes = None
            if isinstance(err, ValueError):
                await self._hass.async_add_executor_job(_remove_cache)

        if self._lookup.prefixes:
            return True

        self._lookup.prefixes = None
        self._retry_at = monotonic() + RETRY_INTERVAL
        return False

    async def lookup(self, mac: str) -> str:
        """Return the vendor for a MAC address, or "" if unknown."""
        if not await self._async_load():
            return ""
        try:
            return await self._lookup.lookup(mac)
        except Exception:
            return ""


def _remove_cache() -> None:
    """Remove a cache file that could not be parsed."""
    try:
        os.unlink(BaseMacLookup.cache_path)
    except OSError:
        pass
