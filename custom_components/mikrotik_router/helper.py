"""Helper functions for Mikrotik Router."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from datetime import timedelta

from homeassistant.util import slugify

from .const import (
    ALL_CHANGES,
    CONF_ALLOWED_CHANGES,
    CONF_TRACK_HOSTS_TIMEOUT,
    DEFAULT_ALLOWED_CHANGES,
    DEFAULT_PORT,
    DEFAULT_TRACK_HOST_TIMEOUT,
    DOMAIN,
    MAX_TRACK_HOST_TIMEOUT,
)

INVALID_ROUTER_IDENTIFIERS = {"", "n/a", "none", "unknown"}


# ---------------------------
#   track_hosts_timeout
# ---------------------------
def track_hosts_timeout(options: Mapping) -> timedelta:
    """Return the host presence timeout, clamped to a sane range."""
    try:
        seconds = int(options.get(CONF_TRACK_HOSTS_TIMEOUT, DEFAULT_TRACK_HOST_TIMEOUT))
    except (TypeError, ValueError):
        seconds = DEFAULT_TRACK_HOST_TIMEOUT
    return timedelta(seconds=min(max(seconds, 1), MAX_TRACK_HOST_TIMEOUT))


# ---------------------------
#   child_device_identifier
# ---------------------------
def child_device_identifier(
    entry_id: str, group: str, key: str | None = None
) -> tuple[str, str]:
    """Return the identifier of a child device of the router device."""
    suffix = slugify(group) if key is None else f"{slugify(group)}-{key}"
    return (DOMAIN, f"{entry_id}-{suffix}")


# ---------------------------
#   format_attribute
# ---------------------------
def format_attribute(attr):
    res = attr.replace("-", "_")
    res = res.replace(" ", "_")
    res = res.lower()
    return res


# ---------------------------
#   allowed_changes
# ---------------------------
def allowed_changes(options: Mapping) -> frozenset[str]:
    """Return the kinds of change Home Assistant may make on the router."""
    if CONF_ALLOWED_CHANGES not in options:
        return frozenset(DEFAULT_ALLOWED_CHANGES)
    value = options[CONF_ALLOWED_CHANGES]
    if not isinstance(value, (list, tuple, set, frozenset)):
        # Malformed (None, a string, ...): fail closed, allow nothing
        return frozenset()
    return frozenset(item for item in value if item in ALL_CHANGES)


# ---------------------------
#   format_data_rate
# ---------------------------
_DATA_RATE_UNITS = ("bit/s", "kbit/s", "Mbit/s", "Gbit/s", "Tbit/s")


def format_data_rate(bits_per_second) -> str | None:
    """Format a bit rate with an auto-scaled unit, such as "12.3 Mbit/s"."""
    if not isinstance(bits_per_second, (int, float)) or isinstance(
        bits_per_second, bool
    ):
        return None
    value = float(bits_per_second)
    unit = 0
    while abs(value) >= 1000 and unit < len(_DATA_RATE_UNITS) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{value:.0f} {_DATA_RATE_UNITS[0]}"
    return f"{value:.3g} {_DATA_RATE_UNITS[unit]}"


# ---------------------------
#   format_value
# ---------------------------
def format_value(res):
    res = res.replace("dhcp", "DHCP")
    res = res.replace("dns", "DNS")
    res = res.replace("capsman", "CAPsMAN")
    res = res.replace("wireless", "Wireless")
    res = res.replace("restored", "Restored")
    return res


# ---------------------------
#   normalize_routeros_version
# ---------------------------
def normalize_routeros_version(version: str) -> str:
    """Strip channel suffixes like '7.23 (stable)' for version comparisons."""
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(version))
    if not match:
        raise ValueError(f"Version format is not recognized: {version}")
    return match.group(1)


# ---------------------------
#   routeros_release_name
# ---------------------------
ROUTEROS_RELEASE_PATTERN = re.compile(
    r"(\d+\.\d+(?:\.\d+)?)((?:alpha|beta|rc)\d+)?", re.IGNORECASE
)


def routeros_release_name(version: str) -> tuple[str, str]:
    """Return (release, prerelease suffix), e.g. '7.20rc2' -> ('7.20', 'rc2').

    The suffix is empty for stable releases. Unlike normalize_routeros_version,
    this keeps rc/beta builds apart for changelog URLs.
    """
    match = ROUTEROS_RELEASE_PATTERN.search(str(version))
    if not match:
        raise ValueError(f"Version format is not recognized: {version}")
    return match.group(1), (match.group(2) or "").lower()


# ---------------------------
#   parse_routeros_major_minor
# ---------------------------
def parse_routeros_major_minor(version: str) -> tuple[int, int]:
    """Return major/minor integers from a RouterOS version string."""
    parts = normalize_routeros_version(version).split(".")
    return int(parts[0]), int(parts[1])


# ---------------------------
#   normalize_router_host
# ---------------------------
def normalize_router_host(host: str) -> str:
    """Normalize a router host for fallback identity matching."""
    normalized_host = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(normalized_host).compressed
    except ValueError:
        return normalized_host.rstrip(".").casefold()


# ---------------------------
#   router_unique_id
# ---------------------------
def router_unique_id(
    host: str,
    port: int = DEFAULT_PORT,
    use_ssl: bool = False,
    serial_number=None,
    software_id=None,
    system_id=None,
) -> str:
    """Return the best available unique ID for a router."""
    for prefix, identifier in (
        ("routerboard", serial_number),
        ("routeros-system-id", system_id),
        ("routeros-software-id", software_id),
    ):
        if identifier is None:
            continue

        normalized_identifier = str(identifier).strip().casefold()
        if normalized_identifier not in INVALID_ROUTER_IDENTIFIERS:
            return f"{prefix}:{normalized_identifier}"

    effective_port = port or (8729 if use_ssl else 8728)
    return f"endpoint:{normalize_router_host(host)}|{effective_port}"
