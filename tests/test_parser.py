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


def test_from_entry_keeps_bools():
    """Item 18: bools are not coerced to ints when the default is numeric."""
    assert apiparser.from_entry({"a": True}, "a", default=0) is True
    assert apiparser.from_entry({"a": 5}, "a", default=0) == 5


def test_get_uid_falls_back_to_secondary_key():
    """Item 19: an empty primary key falls back to the secondary key."""
    assert apiparser.get_uid({"name": "", "id": "x"}, "name", "id", None, None) == "x"
    assert apiparser.get_uid({"name": "n", "id": "x"}, "name", "id", None, None) == "n"
    assert apiparser.get_uid({"name": ""}, "name", None, None, None) is None
    assert apiparser.get_uid({"id": ""}, "name", "id", None, None) is None


def test_netwatch_unknown_status_is_none():
    """M7: an "unknown" netwatch status is not reported as up."""
    data = parse_api(
        data={},
        source=[
            {"host": "a", "status": "up"},
            {"host": "b", "status": "down"},
            {"host": "c", "status": "unknown"},
            {"host": "d"},
        ],
        key="host",
        vals=[{"name": "host"}, {"name": "status", "type": "bool", "default": None}],
    )
    assert [data[h]["status"] for h in "abcd"] == [True, False, None, None]
