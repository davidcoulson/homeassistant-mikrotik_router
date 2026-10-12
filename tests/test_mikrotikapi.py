"""Regression tests for optional RouterOS queries."""

from unittest.mock import MagicMock

import pytest
from librouteros.exceptions import MultiTrapError, TrapError

from custom_components.mikrotik_router.mikrotikapi import MikrotikAPI


@pytest.mark.parametrize("stage", ["path", "list", "command"])
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("ignore_trap", [False, True])
def test_query_traps(stage, multiple, ignore_trap):
    """Optional traps leave the connection usable; required queries still fail."""
    error = TrapError("no such command or directory (wifi)")
    if multiple:
        error = MultiTrapError(error, TrapError("no such command prefix"))

    api = MikrotikAPI("router.example", "test", "test", use_ssl=False)
    connection = MagicMock()
    api._connection = connection
    api._connected = True
    response = connection.path.return_value
    if stage == "path":
        connection.path.side_effect = error
    elif stage == "list":
        response.__iter__.side_effect = error
    else:
        response.return_value.__iter__.side_effect = error

    assert (
        api.query(
            "/interface/wifi",
            command="print" if stage == "command" else None,
            ignore_trap=ignore_trap,
        )
        is None
    )
    assert api.connected() is ignore_trap

    # A subsequent supported query must work without another login.
    if ignore_trap:
        connection.path.side_effect = None
        connection.path.return_value = [{"name": "ether1"}]
        assert api.query("/interface") == [{"name": "ether1"}]
        assert api._connection is connection


@pytest.mark.parametrize("stage", ["path", "list", "command"])
def test_optional_query_transport_error_disconnects(stage):
    """Ignoring RouterOS traps must not hide a broken transport."""
    api = MikrotikAPI("router.example", "test", "test", use_ssl=False)
    connection = MagicMock()
    api._connection = connection
    api._connected = True
    error = ConnectionError("Connection closed")
    response = connection.path.return_value
    if stage == "path":
        connection.path.side_effect = error
    elif stage == "list":
        response.__iter__.side_effect = error
    else:
        response.return_value.__iter__.side_effect = error

    assert (
        api.query(
            "/interface/wifi",
            command="print" if stage == "command" else None,
            ignore_trap=True,
        )
        is None
    )
    assert not api.connected()


def _connected_api(**kwargs):
    api = MikrotikAPI("router.example", "test", "test", **kwargs)
    connection = MagicMock()
    api._connection = connection
    api._connected = True
    return api, connection


def test_ssl_verify_checks_hostname(monkeypatch):
    """H2: with verification on, the certificate must match the configured host."""
    context = MagicMock()
    monkeypatch.setattr(
        "custom_components.mikrotik_router.mikrotikapi.ssl.create_default_context",
        lambda: context,
    )
    api = MikrotikAPI("router.example", "test", "test", use_ssl=True, ssl_verify=True)
    wrap = api._build_ssl_wrapper()
    assert context.check_hostname is True
    sock = object()
    wrap(sock)
    context.wrap_socket.assert_called_once_with(sock, server_hostname="router.example")

    context = MagicMock()
    api = MikrotikAPI("router.example", "test", "test", use_ssl=True, ssl_verify=False)
    monkeypatch.setattr(
        "custom_components.mikrotik_router.mikrotikapi.ssl.create_default_context",
        lambda: context,
    )
    assert api._build_ssl_wrapper() is context.wrap_socket
    assert context.check_hostname is False


def test_connect_passes_timeout(monkeypatch):
    """Coordinator item 7: the socket timeout outlasts check-for-updates."""
    connect = MagicMock()
    monkeypatch.setattr(
        "custom_components.mikrotik_router.mikrotikapi.librouteros.connect", connect
    )
    api = MikrotikAPI("router.example", "test", "test", use_ssl=False)
    assert api.connect()
    assert connect.call_args.kwargs["timeout"] == 15


def test_disconnect_and_close_close_socket(monkeypatch):
    """M3 / item 9: sockets are closed, and a closed API never reconnects."""
    api, connection = _connected_api(use_ssl=False)
    api.disconnect("test", "boom")
    connection.close.assert_called_once()
    assert api._connection is None

    api, connection = _connected_api(use_ssl=False)
    connect = MagicMock()
    monkeypatch.setattr(
        "custom_components.mikrotik_router.mikrotikapi.librouteros.connect", connect
    )
    api.close()
    connection.close.assert_called_once()
    assert not api.connection_check()
    assert not api.connect()
    connect.assert_not_called()


def test_arp_ping_returns_none_on_api_error(monkeypatch):
    """Item 10: an API failure is not reported as the host being away."""
    monkeypatch.setattr(
        "custom_components.mikrotik_router.mikrotikapi.librouteros.connect",
        MagicMock(side_effect=OSError("unreachable")),
    )
    api, connection = _connected_api(use_ssl=False)
    connection.path.return_value.return_value = [{"received": 0}]
    assert api.arp_ping("192.0.2.5", "bridge") is False
    connection.path.return_value.return_value = [{"received": 2}]
    assert api.arp_ping("192.0.2.5", "bridge") is True

    connection.path.return_value.side_effect = ConnectionError("closed")
    assert api.arp_ping("192.0.2.5", "bridge") is None
    assert not api.connected()

    # Reconnect fails: still an API error
    assert api.arp_ping("192.0.2.5", "bridge") is None


@pytest.mark.parametrize("connects", [False, True])
def test_validate_input_closes_api(monkeypatch, connects):
    """M3: the config flow closes its probe connection."""
    from custom_components.mikrotik_router import config_flow

    api = MagicMock()
    api.connect.return_value = connects
    api.connected.return_value = False
    api.error = "cannot_connect"
    monkeypatch.setattr(config_flow, "MikrotikAPI", MagicMock(return_value=api))
    result = config_flow.validate_input(
        {
            "host": "192.0.2.1",
            "username": "u",
            "password": "p",
            "port": 0,
            "ssl": False,
            "verify_ssl": False,
        }
    )
    assert result == (None, "cannot_connect")
    api.close.assert_called_once()
