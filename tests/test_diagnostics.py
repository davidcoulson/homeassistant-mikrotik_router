"""Diagnostics and debug-log redaction (M2, coordinator item 26)."""

import logging
from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED

from custom_components.mikrotik_router.apiparser import parse_api
from custom_components.mikrotik_router.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redact_private_values(hass) -> None:
    """Environment values, DNS names, restored host names and comments are hidden."""
    entry = MagicMock()
    entry.data = {"host": "192.0.2.1", "name": "Mikrotik"}
    entry.options = {}
    entry.runtime_data.data_coordinator.data = {
        "environment": {"token": {"name": "token", "value": "s3cret"}},
        "dns": {"nas.home": {"name": "nas.home", "address": "192.0.2.9", "comment": "Bob"}},
        "host_hass": {"AA:BB:CC:DD:EE:01": "alice-phone"},
        "interface": {"ether1": {"name": "ether1", "comment": "Kitchen"}},
    }
    entry.runtime_data.tracker_coordinator.data = None

    result = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(result)

    for secret in ("s3cret", "nas.home", "alice-phone", "Kitchen", "Bob"):
        assert secret not in text
    data = result["data"]
    assert data["environment"]["token"]["value"] == REDACTED
    assert data["interface"]["ether1"]["name"] == "ether1"


def test_parse_api_debug_log_redacts_extra_keys(caplog) -> None:
    """M2: environment values don't reach debug logs."""
    logger = logging.getLogger("custom_components.mikrotik_router.apiparser")
    logger.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            parse_api(
                data={},
                source=[{"name": "token", "value": "s3cret"}],
                key="name",
                vals=[{"name": "name"}, {"name": "value"}],
                redact={"value"},
            )
    finally:
        logger.setLevel(logging.NOTSET)
    assert "Processing entry" in caplog.text
    assert "s3cret" not in caplog.text
