"""Tests for the RouterOS API parser helpers."""

from datetime import UTC, datetime

from custom_components.mikrotik_router import apiparser, coordinator
from custom_components.mikrotik_router.apiparser import parse_api


def test_utc_from_timestamp_is_aware():
    """Coordinator item 17: timestamps convert without pytz or utcfromtimestamp."""
    expected = datetime(2024, 1, 1, tzinfo=UTC)
    for module in (apiparser, coordinator):
        value = module.utc_from_timestamp(expected.timestamp())
        assert value == expected
        assert value.tzinfo is UTC
    assert "pytz" not in vars(apiparser)
    assert "pytz" not in vars(coordinator)


def test_parse_api_converts_millisecond_timestamps():
    data = parse_api(
        data={},
        source=[{"t": 1704067200000}],
        vals=[{"name": "t", "default": 0, "convert": "utc_from_timestamp"}],
    )
    assert data["t"] == datetime(2024, 1, 1, tzinfo=UTC)
