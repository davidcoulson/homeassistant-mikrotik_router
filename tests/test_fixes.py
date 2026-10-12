"""Regression tests for parser pruning and release notes fetching."""

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
