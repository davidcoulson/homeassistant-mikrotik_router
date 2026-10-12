"""Regression tests for parser pruning and release notes fetching."""

import pytest

from custom_components.mikrotik_router.apiparser import parse_api
from custom_components.mikrotik_router.update import fetch_changelogs


def _nat(source, data):
    return parse_api(
        data=data,
        source=source,
        key=".id",
        vals=[{"name": ".id"}, {"name": "action"}],
        only=[{"key": "action", "value": "dst-nat"}],
        prune=True,
    )


def test_prune_removes_deleted_and_filtered_rows():
    data = _nat([{".id": "*1", "action": "dst-nat"}, {".id": "*2", "action": "dst-nat"}], {})
    assert set(data) == {"*1", "*2"}
    data = _nat([{".id": "*1", "action": "dst-nat"}, {".id": "*2", "action": "masquerade"}], data)
    assert set(data) == {"*1"}
    assert _nat([], data) == {}


def test_prune_keeps_data_on_failed_query():
    data = _nat([{".id": "*1", "action": "dst-nat"}], {})
    assert set(_nat(None, data)) == {"*1"}


class _Response:
    def __init__(self, status, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, published):
        self.published = published
        self.requested = []

    def get(self, url):
        version = url.split("/")[-2]
        self.requested.append(version)
        if version in self.published:
            return _Response(200, f"*) {version}")
        return _Response(404)


async def test_fetch_changelogs_only_requests_existing_releases():
    session = _Session({"7.19.4", "7.19.5", "7.20", "7.20.1", "7.20.2", "7.21", "7.21.1"})
    changelogs = await fetch_changelogs(session, "7.19.4", "7.21.1 (stable)")
    assert changelogs == ["- 7.21.1", "- 7.21", "- 7.20.2", "- 7.20.1", "- 7.20", "- 7.19.5"]
    assert len(session.requested) < 15


async def test_fetch_changelogs_nothing_newer():
    session = _Session(set())
    assert await fetch_changelogs(session, "7.21", "7.21") == []
    assert session.requested == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(None, 180), (0, 1), (-5, 1), (10**12, 86400), (300.0, 300), ("bad", 180)],
)
def test_track_hosts_timeout_is_clamped(value, seconds):
    """M9: stored zero, negative or huge timeouts are clamped."""
    from datetime import timedelta

    from custom_components.mikrotik_router.helper import track_hosts_timeout

    options = {} if value is None else {"track_network_hosts_timeout": value}
    assert track_hosts_timeout(options) == timedelta(seconds=seconds)


async def test_configured_instances_ignores_case(hass):
    """M8: names that differ only in case would share entity unique ids."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.mikrotik_router.config_flow import configured_instances
    from custom_components.mikrotik_router.const import DOMAIN

    MockConfigEntry(domain=DOMAIN, data={"name": "Office"}).add_to_hass(hass)
    assert "office" in configured_instances(hass)


def test_extra_attributes_do_not_linger() -> None:
    """L5: attributes that leave the data are dropped."""
    from unittest.mock import MagicMock

    from custom_components.mikrotik_router.entity import MikrotikEntity
    from custom_components.mikrotik_router.switch_types import SENSOR_TYPES

    description = next(d for d in SENSOR_TYPES if d.key == "nat")
    row = {
        ".id": "*1",
        "name": "tcp:443",
        "comment": "",
        "enabled": True,
        "dst-port": "443",
    }
    coordinator = MagicMock()
    coordinator.data = {"nat": {"*1": row}}
    coordinator.config_entry.data = {"name": "Mikrotik"}
    entity = MikrotikEntity(coordinator, description, "*1")
    assert entity.extra_state_attributes["dst_port"] == "443"
    del row["dst-port"]
    assert "dst_port" not in entity.extra_state_attributes
