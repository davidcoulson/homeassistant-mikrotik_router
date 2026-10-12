"""Item 16: the MAC vendor lookup backs off after a failed load."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.mikrotik_router import vendor_lookup
from custom_components.mikrotik_router.vendor_lookup import VendorLookup


async def test_failed_download_backs_off(hass: HomeAssistant) -> None:
    lookup = VendorLookup(hass)
    clock = [1000.0]
    with patch.object(vendor_lookup, "monotonic", lambda: clock[0]):
        lookup._lookup.load_vendors = AsyncMock(side_effect=OSError("offline"))
        assert await lookup.lookup("00:11:22:33:44:55") == ""
        assert await lookup.lookup("00:11:22:33:44:66") == ""
        assert lookup._lookup.load_vendors.await_count == 1

        clock[0] += vendor_lookup.RETRY_INTERVAL

        async def load():
            lookup._lookup.prefixes = {b"001122": b"Acme"}

        lookup._lookup.load_vendors = AsyncMock(side_effect=load)
        assert await lookup.lookup("00:11:22:33:44:55") == "Acme"
        assert await lookup.lookup("aa:bb:cc:00:00:00") == ""
        assert await lookup.lookup("not-a-mac") == ""


async def test_broken_cache_is_removed(hass: HomeAssistant) -> None:
    lookup = VendorLookup(hass)
    lookup._lookup.load_vendors = AsyncMock(side_effect=ValueError("truncated"))
    with patch.object(vendor_lookup, "_remove_cache") as remove:
        assert await lookup.lookup("00:11:22:33:44:55") == ""
    remove.assert_called_once()
