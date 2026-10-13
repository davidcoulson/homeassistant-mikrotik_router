"""Mikrotik coordinator."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from time import monotonic

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass
from ipaddress import ip_address, IPv4Network

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow

from .helper import (
    allowed_changes,
    is_sfp_port,
    parse_routeros_major_minor,
    track_hosts_timeout,
)


from homeassistant.const import (
    CONF_NAME,
    CONF_HOST,
    CONF_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SSL,
    CONF_VERIFY_SSL,
)

from .const import (
    DOMAIN,
    CONF_TRACK_IFACE_CLIENTS,
    DEFAULT_TRACK_IFACE_CLIENTS,
    CONF_TRACK_HOSTS,
    DEFAULT_TRACK_HOSTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    CONF_DEVICE_TRACKER_SCAN_INTERVAL,
    DEFAULT_DEVICE_TRACKER_SCAN_INTERVAL,
    CONF_SENSOR_PORT_TRAFFIC,
    CHANGE_ACCOUNTING,
    CHANGE_FIRMWARE,
    DEFAULT_SENSOR_PORT_TRAFFIC,
    CONF_SENSOR_CLIENT_TRAFFIC,
    DEFAULT_SENSOR_CLIENT_TRAFFIC,
    CONF_SENSOR_CLIENT_CAPTIVE,
    DEFAULT_SENSOR_CLIENT_CAPTIVE,
    CONF_SENSOR_SIMPLE_QUEUES,
    DEFAULT_SENSOR_SIMPLE_QUEUES,
    CONF_SENSOR_NAT,
    DEFAULT_SENSOR_NAT,
    CONF_SENSOR_MANGLE,
    DEFAULT_SENSOR_MANGLE,
    CONF_SENSOR_FILTER,
    DEFAULT_SENSOR_FILTER,
    CONF_SENSOR_KIDCONTROL,
    DEFAULT_SENSOR_KIDCONTROL,
    CONF_SENSOR_PPP,
    DEFAULT_SENSOR_PPP,
    CONF_SENSOR_SCRIPTS,
    DEFAULT_SENSOR_SCRIPTS,
    CONF_SENSOR_ENVIRONMENT,
    DEFAULT_SENSOR_ENVIRONMENT,
    CONF_SENSOR_NETWATCH_TRACKER,
    DEFAULT_SENSOR_NETWATCH_TRACKER,
)
from .apiparser import parse_api
from .mikrotikapi import MikrotikAPI
from .vendor_lookup import VendorLookup

_LOGGER = logging.getLogger(__name__)

def is_valid_ip(address):
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return False


ETHERNET_MONITOR_VALS = [
    {"name": "status", "default": "unknown"},
    {"name": "rate", "default": "unknown"},
    {"name": "full-duplex", "default": "unknown"},
    {"name": "auto-negotiation", "default": "unknown"},
]
ETHERNET_MONITOR_SFP_VALS = [
    {"name": "status", "default": "unknown"},
    {"name": "auto-negotiation", "default": "unknown"},
    {"name": "rate", "default": "unknown"},
    {"name": "full-duplex", "default": "unknown"},
    {"name": "advertising", "default": "unknown"},
    {"name": "link-partner-advertising", "default": "unknown"},
    {"name": "sfp-temperature", "default": None},
    {"name": "sfp-supply-voltage", "default": "unknown"},
    {"name": "sfp-module-present", "default": "unknown"},
    {"name": "sfp-tx-bias-current", "default": "unknown"},
    {"name": "sfp-tx-power", "default": "unknown"},
    {"name": "sfp-rx-power", "default": "unknown"},
    {"name": "sfp-rx-loss", "default": "unknown"},
    {"name": "sfp-tx-fault", "default": "unknown"},
    {"name": "sfp-type", "default": "unknown"},
    {"name": "sfp-connector-type", "default": "unknown"},
    {"name": "sfp-vendor-name", "default": "unknown"},
    {"name": "sfp-vendor-part-number", "default": "unknown"},
    {"name": "sfp-vendor-revision", "default": "unknown"},
    {"name": "sfp-vendor-serial", "default": "unknown"},
    {"name": "sfp-manufacturing-date", "default": "unknown"},
    {"name": "eeprom-checksum", "default": "unknown"},
]


SLOW_POLL_INTERVAL = 300
FIRMWARE_CHECK_INTERVAL = 4 * 60 * 60

# Interface throughput is the counter delta over a window of about a minute
# (SNMP-style), whatever the scan interval. A poll up to 10% early still
# closes the window, so a 30 s scan publishes every second poll.
TRAFFIC_RATE_INTERVAL = 60
TRAFFIC_RATE_TOLERANCE = 0.9

# /interface counters published once per traffic window, as
# (RouterOS field, raw value key, published key)
INTERFACE_COUNTERS = (
    ("rx-byte", "rx-current", "rx-total"),
    ("tx-byte", "tx-current", "tx-total"),
    ("rx-packet", "rx-packet-current", "rx-packets"),
    ("tx-packet", "tx-packet-current", "tx-packets"),
    ("rx-error", "rx-error-current", "rx-errors"),
    ("tx-error", "tx-error-current", "tx-errors"),
    ("rx-drop", "rx-drop-current", "rx-drops"),
    ("tx-drop", "tx-drop-current", "tx-drops"),
    ("tx-queue-drop", "tx-queue-drop-current", "tx-queue-drops"),
)

# Switch-chip ports (CRS3xx and others) leave the error and drop counters
# out of /interface. Their /interface/ethernet stats count the same events
# per cause; each raw counter is the sum of the causes the router reports.
ETHERNET_ERROR_STATS = {
    "rx-error-current": (
        "rx-fcs-error",
        "rx-align-error",
        "rx-code-error",
        "rx-fragment",
        "rx-jabber",
        "rx-too-short",
        "rx-too-long",
        "rx-length-error",
        "rx-carrier-error",
    ),
    "tx-error-current": (
        "tx-underrun",
        "tx-excessive-collision",
        "tx-late-collision",
        "tx-fcs-error",
        "tx-carrier-sense-error",
    ),
    "rx-drop-current": ("rx-drop", "rx-overflow"),
    "tx-drop-current": ("tx-drop",),
}

_LINK_RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)bps\s*$", re.IGNORECASE)
_RATE_MULTIPLIER = {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3, "T": 1e6}
_ROUTEROS_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%b/%d/%Y %H:%M:%S")

HYSTERESIS_DELTA = 2
HYSTERESIS_MAX_AGE = 300


def _round_rate(rate: float) -> int:
    """Round a rate to 3 significant figures.

    Throughput jitters in the low digits every poll; this keeps the shown
    precision (0.1 kB/s up to 999.9 kB/s) and lets steady rates repeat.
    """
    rate = round(rate)
    digits = len(str(abs(rate)))
    return rate if digits <= 3 else round(rate, 3 - digits)


def _counter_delta(current, previous) -> int | None:
    """Return how far a counter moved, or None when it can't be known.

    A counter below its previous value was reset (reboot, reset-counters)
    or wrapped. Then the bytes since the reset are the best estimate, which
    never shows a spike.
    """
    if current is None or previous is None:
        return None
    return current if current < previous else current - previous


def _link_speed(rate) -> float | None:
    """Turn an ethernet monitor rate such as "2.5Gbps" into Mbit/s."""
    if not isinstance(rate, str) or not (match := _LINK_RATE.match(rate)):
        return None
    speed = float(match.group(1)) * _RATE_MULTIPLIER[match.group(2).upper()]
    return int(speed) if speed.is_integer() else speed


def _duplex(full_duplex) -> str | None:
    """Return "full" or "half" from the ethernet monitor full-duplex flag."""
    if isinstance(full_duplex, bool):
        return "full" if full_duplex else "half"
    return None


_GMT_OFFSET = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")


def _router_time_zone(clock) -> tzinfo | None:
    """Return the router's time zone from a /system/clock row, or None.

    Uses the IANA name when it is known here, else the current GMT offset.
    """
    if not isinstance(clock, dict):
        return None
    name = clock.get("time-zone-name")
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    offset = clock.get("gmt-offset")
    if isinstance(offset, int) and not isinstance(offset, bool):
        return timezone(timedelta(seconds=offset))
    if isinstance(offset, str) and (match := _GMT_OFFSET.match(offset.strip())):
        delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
        return timezone(-delta if match.group(1) == "-" else delta)
    return None


def _parse_routeros_time(value, tz: tzinfo | None) -> datetime | None:
    """Parse a RouterOS local time ("2024-01-05 10:22:33" or "jan/05/2024 ...").

    RouterOS reports the router's local time without a zone, so the router's
    time zone (from /system/clock) is needed; without it the time is unknown.
    """
    if tz is None or not isinstance(value, str) or not value:
        return None
    for fmt in _ROUTEROS_TIME_FORMATS:
        try:
            parsed = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tz)
    return None


def _split_pair(value, convert, default=0) -> tuple:
    """Split an upload/download pair such as "1000/2000".

    Unexpected formats give the default instead of failing the poll.
    """
    parts = str(value).split("/")
    if len(parts) != 2:
        return default, default
    try:
        return convert(parts[0]), convert(parts[1])
    except ValueError:
        return default, default


def _address_in_networks(address, networks) -> bool:
    """Return True if the address is in one of the networks."""
    try:
        address = ip_address(address)
    except ValueError:
        return False
    return any(address in network for network in networks)


def utc_from_timestamp(timestamp: float) -> datetime:
    """Return a UTC time from a timestamp."""
    return datetime.fromtimestamp(timestamp, tz=UTC)


@dataclass
class MikrotikData:
    """Data for the mikrotik integration."""

    data_coordinator: MikrotikCoordinator
    tracker_coordinator: MikrotikTrackerCoordinator
    router_device_id: str
    router_identifier: tuple[str, str] | None = None


type MikrotikConfigEntry = ConfigEntry[MikrotikData]


class MikrotikTrackerCoordinator(DataUpdateCoordinator[None]):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MikrotikConfigEntry,
        coordinator: MikrotikCoordinator,
    ):
        """Initialize MikrotikTrackerCoordinator."""
        self.hass = hass
        self.coordinator = coordinator
        self.command_lock = coordinator.command_lock

        super().__init__(
            self.hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=coordinator.option_device_tracker_scan_interval,
        )
        self.name = config_entry.data[CONF_NAME]
        self.host = config_entry.data[CONF_HOST]

        self.api = MikrotikAPI(
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
            config_entry.data[CONF_PORT],
            config_entry.data[CONF_SSL],
            config_entry.data[CONF_VERIFY_SSL],
            allowed_changes=allowed_changes(config_entry.options),
        )

    # ---------------------------
    #   _async_update_data
    # ---------------------------
    async def _async_update_data(self):
        """Ping wired hosts and return the host snapshot."""
        coordinator = self.coordinator
        if not coordinator.last_update_success:
            # Wireless and CAPsMAN presence comes from the main poll. While it
            # fails, the cached rows are stale: don't keep stamping hosts as
            # just seen. The tracker entities turn unavailable meanwhile.
            raise UpdateFailed("The router poll is failing, host presence unknown")
        hosts = coordinator.ds["host"]
        if coordinator.option_track_network_hosts:
            if not coordinator.host_tracking_initialized:
                # Add missing default values
                for host in list(hosts.values()):
                    for key, default in zip(
                        [
                            "address",
                            "mac-address",
                            "interface",
                            "host-name",
                            "last-seen",
                            "available",
                        ],
                        ["unknown", "unknown", "unknown", "unknown", False, False],
                    ):
                        host.setdefault(key, default)

            # Pinging needs the "test" policy. Without it, wireless, CAPsMAN
            # and DHCP hosts are still tracked from the main poll.
            if "test" in coordinator.ds["access"] and (
                targets := self._ping_targets()
            ):
                results = await self.hass.async_add_executor_job(
                    self._ping_hosts, targets
                )
                now = utcnow()
                for uid, available in results.items():
                    # None means the API failed: keep the previous state
                    if available is None or uid not in hosts:
                        continue
                    hosts[uid]["available"] = available
                    if available:
                        hosts[uid]["last-seen"] = now

            coordinator.host_tracking_initialized = True

        await coordinator.async_process_host()
        return {
            "host": coordinator.ds["host"],
            "routerboard": coordinator.ds["routerboard"],
        }

    # ---------------------------
    #   _ping_targets
    # ---------------------------
    def _ping_targets(self) -> dict[str, tuple[str, str]]:
        """Return uid -> (address, interface) for wired hosts due a ping."""
        # A host seen within half the timeout stays home until the next cycle,
        # so it doesn't need a ping yet.
        fresh_after = utcnow() - self.coordinator.option_track_network_hosts_timeout / 2
        arp = self.coordinator.ds["arp"]
        targets = {}
        for uid, host in list(self.coordinator.ds["host"].items()):
            if (
                host.get("source") in ("capsman", "wireless")
                or host.get("address") in ("unknown", "", None)
                or host.get("interface") in ("unknown", "", None)
            ):
                continue

            last_seen = host.get("last-seen")
            if host.get("available") and last_seen and last_seen > fresh_after:
                continue

            interface = host["interface"]
            if uid in arp and arp[uid].get("bridge", "") != "":
                interface = arp[uid]["bridge"]
            targets[uid] = (host["address"], interface)

        return targets

    # ---------------------------
    #   _ping_hosts
    # ---------------------------
    def _ping_hosts(
        self, targets: dict[str, tuple[str, str]]
    ) -> dict[str, bool | None]:
        """Ping all targets in one executor job."""
        results = {}
        for uid, (address, interface) in targets.items():
            _LOGGER.debug("Ping host: %s", address)
            results[uid] = self.api.arp_ping(address, interface)
            if results[uid] is None and not self.api.connected():
                # Connection lost: the rest would fail too
                break

        return results


# ---------------------------
#   MikrotikControllerData
# ---------------------------
class MikrotikCoordinator(DataUpdateCoordinator[None]):
    """MikrotikCoordinator Class"""

    # Cleared when the router refuses /interface/ethernet "print stats"
    _ethernet_stats_supported = True

    def __init__(self, hass: HomeAssistant, config_entry: MikrotikConfigEntry):
        """Initialize MikrotikCoordinator."""
        self.hass = hass
        self.config_entry: MikrotikConfigEntry = config_entry
        super().__init__(
            self.hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=self.option_scan_interval,
        )
        self.name = config_entry.data[CONF_NAME]
        self.host = config_entry.data[CONF_HOST]

        self.ds = {
            "access": {},
            "routerboard": {},
            "resource": {},
            "health": {},
            "health7": {},
            "interface": {},
            "bonding": {},
            "bonding_slaves": {},
            "bridge": {},
            "bridge_host": {},
            "arp": {},
            "nat": {},
            "kid-control": {},
            "mangle": {},
            "filter": {},
            "ppp_secret": {},
            "ppp_active": {},
            "fw-update": {},
            "script": {},
            "queue": {},
            "dns": {},
            "dhcp-server": {},
            "dhcp-client": {},
            "dhcp-network": {},
            "dhcp": {},
            "capsman_hosts": {},
            "wireless": {},
            "wireless_hosts": {},
            "host": {},
            "host_hass": {},
            "hostspot_host": {},
            "client_traffic": {},
            "environment": {},
            "ups": {},
            "gps": {},
            "netwatch": {},
        }

        self.notified_flags = []

        self.api = MikrotikAPI(
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
            config_entry.data[CONF_PORT],
            config_entry.data[CONF_SSL],
            config_entry.data[CONF_VERIFY_SSL],
            allowed_changes=allowed_changes(config_entry.options),
        )

        self.debug = False
        if _LOGGER.getEffectiveLevel() == 10:
            self.debug = True

        self.host_hass_recovered = False
        self.host_tracking_initialized = False
        # Serialises entity commands (switches, buttons, services)
        self.command_lock = asyncio.Lock()

        self.support_capsman = False
        self.support_wireless = False
        self.support_hotspot = False
        self.support_ppp = False
        self.support_ups = False
        self.support_gps = False
        self._wifimodules = []

        self.major_fw_version = 0
        self.minor_fw_version = 0

        self.async_mac_lookup = VendorLookup(hass)
        self.accessrights_reported = False

        self.last_hwinfo_update = datetime(1970, 1, 1)
        self.interface_traffic_last_run = None
        self._published = {}
        self._monitor_batch = True
        # The router's time zone, read from /system/clock in the slow tier
        self._router_tz = None
        self.rebootcheck = 0
        # Tiered polling: static config is read every SLOW_POLL_INTERVAL,
        # or on the next poll after force_full_refresh is set.
        self.force_full_refresh = True
        self._slow_poll = True
        self._last_slow_poll = None
        self._last_firmware_check = None
        self._dhcp_server_misses = set()
        self._host_lock = asyncio.Lock()

    # ---------------------------
    #   option_track_iface_clients
    # ---------------------------
    @property
    def option_track_iface_clients(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_TRACK_IFACE_CLIENTS, DEFAULT_TRACK_IFACE_CLIENTS
        )

    # ---------------------------
    #   option_track_network_hosts
    # ---------------------------
    @property
    def option_track_network_hosts(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_TRACK_HOSTS, DEFAULT_TRACK_HOSTS)

    # ---------------------------
    #   option_track_network_hosts_timeout
    # ---------------------------
    @property
    def option_track_network_hosts_timeout(self) -> timedelta:
        """Config entry option: how long a host stays home after a reply."""
        return track_hosts_timeout(self.config_entry.options)

    # ---------------------------
    #   option_sensor_port_traffic
    # ---------------------------
    @property
    def option_sensor_port_traffic(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_PORT_TRAFFIC, DEFAULT_SENSOR_PORT_TRAFFIC
        )

    # ---------------------------
    #   option_allowed_changes
    # ---------------------------
    @property
    def option_allowed_changes(self) -> frozenset[str]:
        """Config entry option: the kinds of change allowed on the router."""
        return allowed_changes(self.config_entry.options)

    # ---------------------------
    #   option_sensor_client_traffic
    # ---------------------------
    @property
    def option_sensor_client_traffic(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_CLIENT_TRAFFIC, DEFAULT_SENSOR_CLIENT_TRAFFIC
        )

    # ---------------------------
    #   option_sensor_client_captive
    # ---------------------------
    @property
    def option_sensor_client_captive(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_CLIENT_CAPTIVE, DEFAULT_SENSOR_CLIENT_CAPTIVE
        )

    # ---------------------------
    #   option_sensor_simple_queues
    # ---------------------------
    @property
    def option_sensor_simple_queues(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_SIMPLE_QUEUES, DEFAULT_SENSOR_SIMPLE_QUEUES
        )

    # ---------------------------
    #   option_sensor_nat
    # ---------------------------
    @property
    def option_sensor_nat(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_SENSOR_NAT, DEFAULT_SENSOR_NAT)

    # ---------------------------
    #   option_sensor_mangle
    # ---------------------------
    @property
    def option_sensor_mangle(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_SENSOR_MANGLE, DEFAULT_SENSOR_MANGLE)

    # ---------------------------
    #   option_sensor_filter
    # ---------------------------
    @property
    def option_sensor_filter(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_SENSOR_FILTER, DEFAULT_SENSOR_FILTER)

    # ---------------------------
    #   option_sensor_kidcontrol
    # ---------------------------
    @property
    def option_sensor_kidcontrol(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_KIDCONTROL, DEFAULT_SENSOR_KIDCONTROL
        )

    # ---------------------------
    #   option_sensor_netwatch
    # ---------------------------
    @property
    def option_sensor_netwatch(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_NETWATCH_TRACKER, DEFAULT_SENSOR_NETWATCH_TRACKER
        )

    # ---------------------------
    #   option_sensor_ppp
    # ---------------------------
    @property
    def option_sensor_ppp(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(CONF_SENSOR_PPP, DEFAULT_SENSOR_PPP)

    # ---------------------------
    #   option_sensor_scripts
    # ---------------------------
    @property
    def option_sensor_scripts(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_SCRIPTS, DEFAULT_SENSOR_SCRIPTS
        )

    # ---------------------------
    #   option_sensor_environment
    # ---------------------------
    @property
    def option_sensor_environment(self):
        """Config entry option to not track ARP."""
        return self.config_entry.options.get(
            CONF_SENSOR_ENVIRONMENT, DEFAULT_SENSOR_ENVIRONMENT
        )

    # ---------------------------
    #   option_scan_interval
    # ---------------------------
    @property
    def option_scan_interval(self):
        """Config entry option scan interval."""
        scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return timedelta(seconds=scan_interval)

    # ---------------------------
    #   option_device_tracker_scan_interval
    # ---------------------------
    @property
    def option_device_tracker_scan_interval(self):
        """Config entry option device tracker scan interval."""
        scan_interval = self.config_entry.options.get(
            CONF_DEVICE_TRACKER_SCAN_INTERVAL, DEFAULT_DEVICE_TRACKER_SCAN_INTERVAL
        )
        return timedelta(seconds=scan_interval)

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self):
        """Return connected state"""
        return self.api.connected()

    # ---------------------------
    #   set_value
    # ---------------------------
    def set_value(self, path, param, value, mod_param, mod_value):
        """Change value using Mikrotik API"""
        return self.api.set_value(path, param, value, mod_param, mod_value)

    # ---------------------------
    #   execute
    # ---------------------------
    def execute(self, path, command, param, value, attributes=None):
        """Change value using Mikrotik API"""
        return self.api.execute(path, command, param, value, attributes)

    # ---------------------------
    #   get_capabilities
    # ---------------------------
    def get_capabilities(self):
        """Update Mikrotik data"""
        packages = parse_api(
            data={},
            source=self.api.query("/system/package"),
            key="name",
            vals=[
                {"name": "name"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
        )
        self.support_capsman = False
        self.support_wireless = False
        self.support_hotspot = False
        self._wifimodules = []

        hotspot_package_enabled = (
            "hotspot" in packages and packages["hotspot"]["enabled"]
        )

        if 0 < self.major_fw_version < 7:
            self.support_hotspot = hotspot_package_enabled

            if "ppp" in packages:
                self.support_ppp = packages["ppp"]["enabled"]

            if "wireless" in packages:
                self.support_capsman = packages["wireless"]["enabled"]
                self.support_wireless = packages["wireless"]["enabled"]
                if self.support_wireless:
                    self._wifimodules = ["wireless"]

        elif 0 < self.major_fw_version >= 7:
            self.support_ppp = True
            architecture = str(
                self.ds["resource"].get("architecture-name", "unknown")
            ).lower()
            # RouterOS 7.20 moved Hotspot into a separate package on SMIPS.
            hotspot_separate_package = architecture == "smips" and (
                self.major_fw_version > 7
                or (self.major_fw_version == 7 and self.minor_fw_version >= 20)
            )
            self.support_hotspot = (
                hotspot_package_enabled if hotspot_separate_package else True
            )

            if "wifiwave2" in packages and packages["wifiwave2"]["enabled"]:
                self._wifimodules.append("wifiwave2")

            # SMIPS only exposes the legacy wireless API.
            if architecture != "smips" and (
                (self.major_fw_version == 7 and self.minor_fw_version >= 13)
                or self.major_fw_version > 7
                or any(
                    package in packages and packages[package]["enabled"]
                    for package in ["wifi", "wifi-qcom", "wifi-qcom-ac"]
                )
            ):
                self._wifimodules.append("wifi")

            if (self.major_fw_version == 7 and self.minor_fw_version < 13) or (
                "wireless" in packages and packages["wireless"]["enabled"]
            ):
                self._wifimodules.append("wireless")
                self.support_capsman = True

            self.support_wireless = bool(self._wifimodules)

            _LOGGER.debug(
                "Mikrotik %s wifi modules=%s",
                self.host,
                self._wifimodules,
            )

        if "ups" in packages and packages["ups"]["enabled"]:
            self.support_ups = True

        if "gps" in packages and packages["gps"]["enabled"]:
            self.support_gps = True

        if not self.support_hotspot:
            self.ds["hostspot_host"] = {}
            self.ds["resource"]["captive_authorized"] = 0

    # ---------------------------
    #   async_get_host_hass
    # ---------------------------
    async def async_get_host_hass(self):
        """Get host data from HA entity registry"""
        registry = entity_registry.async_get(self.hass)
        prefix = f"{self.name.lower()}-host-"
        for entity in entity_registry.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if entity.domain != "device_tracker" or not entity.unique_id.startswith(
                prefix
            ):
                continue

            # Unique IDs carry the slugified MAC (aa_bb_cc_dd_ee_ff)
            mac = entity.unique_id[len(prefix) :].replace("_", ":").upper()
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                continue

            self.ds["host_hass"][mac] = entity.original_name

    # ---------------------------
    #   _async_update_data
    # ---------------------------
    async def _async_update_data(self):
        """Update Mikrotik data"""
        now = monotonic()
        self._slow_poll = (
            self.force_full_refresh
            or self._last_slow_poll is None
            or now - self._last_slow_poll >= SLOW_POLL_INTERVAL
        )
        self.force_full_refresh = False
        if self._slow_poll:
            self._dhcp_server_misses.clear()

        resource_read = False
        delta = datetime.now().replace(microsecond=0) - self.last_hwinfo_update
        if self.api.has_reconnected() or delta.total_seconds() > 60 * 60 * 4:
            await self.hass.async_add_executor_job(self.get_access)

            if self.api.connected():
                await self.hass.async_add_executor_job(self.get_system_resource)
                resource_read = True

            if self.api.connected():
                await self.hass.async_add_executor_job(self.get_capabilities)

            if self.api.connected():
                await self.hass.async_add_executor_job(self.get_system_routerboard)

            if self.api.connected():
                await self.hass.async_add_executor_job(self.get_dhcp_network)

            if self.api.connected():
                await self.hass.async_add_executor_job(self.get_dns)

            if not self.api.connected():
                self._raise_connection_error()

            if self.api.connected():
                self.last_hwinfo_update = datetime.now().replace(microsecond=0)

        if not resource_read:
            await self.hass.async_add_executor_job(self.get_system_resource)

        # The firmware check streams for 10 s with the API lock held, so it
        # runs on its own cadence and never during the first refresh.
        if (
            self.api.connected()
            and self.data is not None
            and (
                self._last_firmware_check is None
                or now - self._last_firmware_check >= FIRMWARE_CHECK_INTERVAL
            )
        ):
            await self.hass.async_add_executor_job(self.get_firmware_update)
            self._last_firmware_check = now

        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_system_health)

        if self.api.connected() and self.option_sensor_scripts:
            await self.hass.async_add_executor_job(self.get_script)

        if self.api.connected() and self._slow_poll:
            await self.hass.async_add_executor_job(self.get_dhcp_client)
            await self.hass.async_add_executor_job(self.get_dhcp_server)

        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_interface)

        if (
            self.api.connected()
            and not self.host_hass_recovered
            and not self.ds["host_hass"]
        ):
            await self.async_get_host_hass()

        if self.api.connected() and self.support_capsman:
            await self.hass.async_add_executor_job(self.get_capsman_hosts)

        if self.api.connected() and self.support_wireless and self._slow_poll:
            await self.hass.async_add_executor_job(self.get_wireless)

        if self.api.connected() and self.support_wireless:
            await self.hass.async_add_executor_job(self.get_wireless_hosts)

        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_bridge)

        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_arp)

        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_dhcp)

        if self.api.connected():
            await self.async_process_host()

        if self.api.connected():
            await self.hass.async_add_executor_job(self.process_interface_client)

        if self.api.connected() and self.option_sensor_nat and self._slow_poll:
            await self.hass.async_add_executor_job(self.get_nat)

        if self.api.connected() and self.option_sensor_kidcontrol:
            await self.hass.async_add_executor_job(self.get_kidcontrol)

        if self.api.connected() and self.option_sensor_mangle and self._slow_poll:
            await self.hass.async_add_executor_job(self.get_mangle)

        if self.api.connected() and self.option_sensor_filter and self._slow_poll:
            await self.hass.async_add_executor_job(self.get_filter)

        if self.api.connected() and self.option_sensor_netwatch:
            await self.hass.async_add_executor_job(self.get_netwatch)

        if self.api.connected() and self.support_ppp and self.option_sensor_ppp:
            await self.hass.async_add_executor_job(self.get_ppp)

        if self.api.connected() and self.option_sensor_client_traffic:
            # Accounting needs "snapshot/take", a write
            if (
                0 < self.major_fw_version < 7
                and CHANGE_ACCOUNTING in self.option_allowed_changes
            ):
                await self.hass.async_add_executor_job(self.process_accounting)
            elif 0 < self.major_fw_version >= 7:
                await self.hass.async_add_executor_job(self.process_kid_control_devices)

        if (
            self.api.connected()
            and self.support_hotspot
            and self.option_sensor_client_captive
        ):
            await self.hass.async_add_executor_job(self.get_captive)

        if self.api.connected() and self.option_sensor_simple_queues:
            await self.hass.async_add_executor_job(self.get_queue)

        if self.api.connected() and self.option_sensor_environment:
            await self.hass.async_add_executor_job(self.get_environment)

        if self.api.connected() and self.support_ups:
            await self.hass.async_add_executor_job(self.get_ups)

        if self.api.connected() and self.support_gps:
            await self.hass.async_add_executor_job(self.get_gps)

        if not self.api.connected():
            self.force_full_refresh = True
            self._raise_connection_error()

        if self._slow_poll:
            self._last_slow_poll = now

        # async_dispatcher_send(self.hass, "update_sensors", self)
        return self.ds

    # ---------------------------
    #   async_request_full_refresh
    # ---------------------------
    async def async_request_full_refresh(self) -> None:
        """Refresh soon, including the slow tier (after a config change)."""
        self.force_full_refresh = True
        await self.async_request_refresh()

    # ---------------------------
    #   _raise_connection_error
    # ---------------------------
    def _raise_connection_error(self) -> None:
        """Raise an authentication or temporary connection error."""
        if self.api.error == "wrong_login":
            raise ConfigEntryAuthFailed("Invalid Mikrotik credentials")
        raise UpdateFailed("Mikrotik Disconnected")

    # ---------------------------
    #   get_access
    # ---------------------------
    def get_access(self) -> None:
        """Get access rights from Mikrotik"""
        tmp_user = parse_api(
            data={},
            source=self.api.query("/user"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "group"},
            ],
        )

        tmp_group = parse_api(
            data={},
            source=self.api.query("/user/group"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "policy"},
            ],
        )

        # if tmp_user[self.config_entry.data[CONF_USERNAME]]["group"] in tmp_group:
        current_user = self.config_entry.data.get(CONF_USERNAME)
        if current_user in tmp_user:
            if tmp_user[current_user]["group"] in tmp_group:
                self.ds["access"] = tmp_group[
                    tmp_user[self.config_entry.data[CONF_USERNAME]]["group"]
                ]["policy"].split(",")

        if not self.accessrights_reported:
            self.accessrights_reported = True
            if (
                "write" not in self.ds["access"]
                or "policy" not in self.ds["access"]
                or "reboot" not in self.ds["access"]
                or "test" not in self.ds["access"]
            ):
                _LOGGER.warning(
                    "Mikrotik %s user %s does not have sufficient access rights. Integration functionality will be limited.",
                    self.host,
                    self.config_entry.data[CONF_USERNAME],
                )

    # ---------------------------
    #   get_interface
    # ---------------------------
    def get_interface(self) -> None:
        """Get all interfaces data from Mikrotik"""
        interface_traffic_current_run = monotonic()
        interfaces = self.api.query("/interface")
        self.ds["interface"] = parse_api(
            data=self.ds["interface"],
            prune=True,
            source=interfaces,
            key="default-name",
            key_secondary="name",
            vals=[
                {"name": "default-name"},
                {"name": ".id"},
                {"name": "name", "default_val": "default-name"},
                {"name": "type", "default": "unknown"},
                {"name": "running", "type": "bool"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
                {"name": "port-mac-address", "source": "mac-address"},
                {"name": "comment"},
                {"name": "last-link-down-time"},
                {"name": "last-link-up-time"},
                {"name": "link-downs"},
                {"name": "tx-queue-drop"},
                {"name": "actual-mtu"},
                {"name": "about", "source": ".about", "default": ""},
                *(
                    {"name": raw, "source": field, "default": None}
                    for field, raw, _ in INTERFACE_COUNTERS
                ),
            ],
            ensure_vals=[
                {"name": "client-ip-address"},
                {"name": "client-mac-address"},
                {"name": "rx", "default": None},
                {"name": "tx", "default": None},
                *(
                    {"name": published, "default": None}
                    for _, _, published in INTERFACE_COUNTERS
                ),
            ],
            skip=[
                {"name": "type", "value": "bridge"},
                {"name": "type", "value": "ppp-in"},
                {"name": "type", "value": "pptp-in"},
                {"name": "type", "value": "sstp-in"},
                {"name": "type", "value": "l2tp-in"},
                {"name": "type", "value": "pppoe-in"},
                {"name": "type", "value": "ovpn-in"},
            ],
        )

        if (
            self.option_sensor_port_traffic
            and interfaces is not None
            and self.api.connected()
        ):
            self._update_interface_traffic(interface_traffic_current_run)

        # Ethernet config (PoE, SFP shutdown temperature) rarely changes:
        # read it in the slow tier, or when a new ether port appears.
        if self._slow_poll or any(
            vals["type"] == "ether" and "sfp-shutdown-temperature" not in vals
            for vals in self.ds["interface"].values()
        ):
            self._get_ethernet_config()

        # Link-change times are in the router's local time
        if self._slow_poll:
            self._get_router_time_zone()

        # Udpate virtual interfaces
        bonding = False
        sfp_ports = []
        copper_ports = []
        for uid, vals in self.ds["interface"].items():
            if self.ds["interface"][uid]["type"] == "bond":
                bonding = True

            self.ds["interface"][uid]["comment"] = str(
                self.ds["interface"][uid]["comment"]
            )

            if vals["default-name"] == "":
                self.ds["interface"][uid]["default-name"] = vals["name"]
                self.ds["interface"][uid][
                    "port-mac-address"
                ] = f"{vals['port-mac-address']}-{vals['name']}"

            if vals["type"] == "ether" and vals.get(".id"):
                if is_sfp_port(vals):
                    sfp_ports.append(vals[".id"])
                else:
                    copper_ports.append(vals[".id"])

        if self._slow_poll:
            # One refused batch (a port .id removed between polls) only costs
            # per-port calls until the next slow tier, then batching is retried
            self._monitor_batch = True
        self._monitor_ethernet(sfp_ports, ETHERNET_MONITOR_SFP_VALS)
        self._monitor_ethernet(copper_ports, ETHERNET_MONITOR_VALS)

        router_tz = getattr(self, "_router_tz", None)
        for vals in self.ds["interface"].values():
            vals["link-speed"] = _link_speed(vals.get("rate"))
            vals["duplex"] = _duplex(vals.get("full-duplex"))
            changes = [
                parsed
                for parsed in (
                    _parse_routeros_time(vals.get("last-link-up-time"), router_tz),
                    _parse_routeros_time(vals.get("last-link-down-time"), router_tz),
                )
                if parsed is not None
            ]
            vals["last-link-change"] = max(changes) if changes else None

        if bonding:
            self.ds["bonding"] = parse_api(
                data={},
                source=self.api.query("/interface/bonding"),
                key="name",
                vals=[
                    {"name": "name"},
                    {"name": "mac-address"},
                    {"name": "slaves"},
                    {"name": "mode"},
                ],
            )

            self.ds["bonding_slaves"] = {}
            for uid, vals in self.ds["bonding"].items():
                for tmp in vals["slaves"].split(","):
                    self.ds["bonding_slaves"][tmp] = vals
                    self.ds["bonding_slaves"][tmp]["master"] = uid

    # ---------------------------
    #   _update_interface_traffic
    # ---------------------------
    def _update_interface_traffic(self, current_run: float) -> None:
        """Publish interface throughput and counters once per traffic window.

        Throughput is the byte-counter delta over the window in bit/s, like
        an SNMP poller, not RouterOS's instantaneous rate. Between windows
        the published values stay put, so the recorder sees one change a
        minute at most.
        """
        last_run = self.interface_traffic_last_run
        if (
            last_run is not None
            and current_run - last_run < TRAFFIC_RATE_INTERVAL * TRAFFIC_RATE_TOLERANCE
        ):
            return

        time_diff = current_run - last_run if last_run is not None else 0
        self.interface_traffic_last_run = current_run
        self._fill_ethernet_error_stats()

        for vals in self.ds["interface"].values():
            for direction in ("rx", "tx"):
                current = vals.get(f"{direction}-current")
                delta = _counter_delta(current, vals.get(f"{direction}-previous"))
                vals[direction] = (
                    _round_rate(delta * 8 / time_diff)
                    if delta is not None and time_diff
                    else None
                )
                vals[f"{direction}-previous"] = current

            for _, raw, published in INTERFACE_COUNTERS:
                vals[published] = vals.get(raw)

    # ---------------------------
    #   _fill_ethernet_error_stats
    # ---------------------------
    def _fill_ethernet_error_stats(self) -> None:
        """Fill error and drop counters /interface left out of ethernet ports.

        One "print stats" call, only in a traffic window where some ethernet
        port lacks them. A router that refuses it isn't asked again.
        """
        if not self._ethernet_stats_supported:
            return
        missing = {
            vals.get("name"): vals
            for vals in self.ds["interface"].values()
            if vals.get("type") == "ether"
            and any(vals.get(raw) is None for raw in ETHERNET_ERROR_STATS)
        }
        if not missing:
            return

        source = self.api.query(
            "/interface/ethernet",
            command="print",
            args={"stats": ""},
            ignore_trap=True,
        )
        if source is None:
            if self.api.connected():
                _LOGGER.debug("Ethernet stats unavailable, not asking again")
                self._ethernet_stats_supported = False
            return

        for row in source:
            vals = missing.get(row.get("name"))
            if vals is None:
                continue
            for raw, fields in ETHERNET_ERROR_STATS.items():
                if vals.get(raw) is not None:
                    continue
                counts = [
                    row[field]
                    for field in fields
                    if isinstance(row.get(field), int)
                    and not isinstance(row.get(field), bool)
                ]
                if counts:
                    vals[raw] = sum(counts)

    # ---------------------------
    #   _get_ethernet_config
    # ---------------------------
    def _get_ethernet_config(self) -> None:
        """Get ethernet port config (PoE, SFP) from Mikrotik"""
        self.ds["interface"] = parse_api(
            data=self.ds["interface"],
            source=self.api.query("/interface/ethernet"),
            key="default-name",
            key_secondary="name",
            vals=[
                {"name": "default-name"},
                {"name": "name", "default_val": "default-name"},
                {"name": "poe-out", "default": "N/A"},
                {"name": "sfp-shutdown-temperature", "default": None},
            ],
            skip=[
                {"name": "type", "value": "bridge"},
                {"name": "type", "value": "ppp-in"},
                {"name": "type", "value": "pptp-in"},
                {"name": "type", "value": "sstp-in"},
                {"name": "type", "value": "l2tp-in"},
                {"name": "type", "value": "pppoe-in"},
                {"name": "type", "value": "ovpn-in"},
            ],
        )

    # ---------------------------
    #   _get_router_time_zone
    # ---------------------------
    def _get_router_time_zone(self) -> None:
        """Read the router's time zone (None when it can't be told)."""
        clock = self.api.query("/system/clock", ignore_trap=True)
        zone = _router_time_zone(clock[0]) if clock else None
        if zone is not None or clock is not None:
            # Keep the last known zone when the read itself failed
            self._router_tz = zone

    # ---------------------------
    #   _monitor_ethernet
    # ---------------------------
    def _monitor_ethernet(self, port_ids, vals) -> None:
        """Read link and SFP status for ethernet ports.

        One monitor call covers all ports; if the router refuses a list,
        fall back to one call per port from then on.
        """
        if not port_ids or not self.api.connected():
            return

        if self._monitor_batch:
            source = self.api.query(
                "/interface/ethernet",
                command="monitor",
                args={"numbers": ",".join(port_ids), "once": True},
                ignore_trap=True,
            )
            if source is not None:
                self.ds["interface"] = parse_api(
                    data=self.ds["interface"],
                    source=source,
                    key_search="name",
                    vals=vals,
                )
                return

            if not self.api.connected():
                return
            _LOGGER.debug("Batched ethernet monitor refused, using one call per port")
            self._monitor_batch = False

        for port_id in port_ids:
            self.ds["interface"] = parse_api(
                data=self.ds["interface"],
                source=self.api.query(
                    "/interface/ethernet",
                    command="monitor",
                    args={".id": port_id, "once": True},
                ),
                key_search="name",
                vals=vals,
            )

    # ---------------------------
    #   get_bridge
    # ---------------------------
    def get_bridge(self) -> None:
        """Get system resources data from Mikrotik"""
        self.ds["bridge_host"] = parse_api(
            # Parse into a copy and swap it in: the tracker reads these
            # tables on the event loop while this runs in the executor (item 3)
            data=dict(self.ds["bridge_host"]),
            prune=True,
            source=self.api.query("/interface/bridge/host"),
            key="mac-address",
            vals=[
                {"name": "mac-address"},
                {"name": "interface", "default": "unknown"},
                {"name": "bridge", "default": "unknown"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            only=[{"key": "local", "value": False}],
        )

        for uid, vals in self.ds["bridge_host"].items():
            self.ds["bridge"][vals["bridge"]] = True

    # ---------------------------
    #   process_interface_client
    # ---------------------------
    def process_interface_client(self) -> None:
        # Remove data if disabled
        if not self.option_track_iface_clients:
            for uid in self.ds["interface"]:
                self.ds["interface"][uid]["client-ip-address"] = "disabled"
                self.ds["interface"][uid]["client-mac-address"] = "disabled"
            return

        # Index ARP by interface once instead of scanning it per interface
        arp_by_interface = {}
        for arp_vals in self.ds["arp"].values():
            arp_by_interface.setdefault(arp_vals["interface"], []).append(arp_vals)

        for uid, vals in self.ds["interface"].items():
            clients = list(arp_by_interface.get(vals["name"], ()))
            if vals["name"] in self.ds["bonding_slaves"]:
                master = self.ds["bonding_slaves"][vals["name"]]["master"]
                if master != vals["name"]:
                    clients += arp_by_interface.get(master, ())

            if not clients:
                vals["client-ip-address"] = ""
                vals["client-mac-address"] = ""
            elif len(clients) == 1:
                vals["client-ip-address"] = clients[0]["address"]
                vals["client-mac-address"] = clients[0]["mac-address"]
            else:
                vals["client-ip-address"] = "multiple"
                vals["client-mac-address"] = "multiple"

            if self.ds["interface"][uid]["client-ip-address"] == "":
                if self.ds["interface"][uid]["name"] in self.ds["dhcp-client"]:
                    self.ds["interface"][uid]["client-ip-address"] = self.ds[
                        "dhcp-client"
                    ][self.ds["interface"][uid]["name"]]["address"]
                else:
                    self.ds["interface"][uid]["client-ip-address"] = "none"

            if self.ds["interface"][uid]["client-mac-address"] == "":
                self.ds["interface"][uid]["client-mac-address"] = "none"

    # ---------------------------
    #   get_nat
    # ---------------------------
    def get_nat(self) -> None:
        """Get NAT data from Mikrotik"""
        self.ds["nat"] = parse_api(
            data=self.ds["nat"],
            prune=True,
            source=self.api.query("/ip/firewall/nat"),
            key=".id",
            vals=[
                {"name": ".id"},
                {"name": "chain", "default": "unknown"},
                {"name": "action", "default": "unknown"},
                {"name": "protocol", "default": "any"},
                {"name": "dst-port", "default": "any"},
                {"name": "in-interface", "default": "any"},
                {"name": "out-interface", "default": "any"},
                {"name": "to-addresses"},
                {"name": "to-ports", "default": "any"},
                {"name": "comment"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            val_proc=[
                [
                    {"name": "uniq-id"},
                    {"action": "combine"},
                    {"key": "chain"},
                    {"text": ","},
                    {"key": "action"},
                    {"text": ","},
                    {"key": "protocol"},
                    {"text": ","},
                    {"key": "in-interface"},
                    {"text": ":"},
                    {"key": "dst-port"},
                    {"text": "-"},
                    {"key": "out-interface"},
                    {"text": ":"},
                    {"key": "to-addresses"},
                    {"text": ":"},
                    {"key": "to-ports"},
                ],
                [
                    {"name": "name"},
                    {"action": "combine"},
                    {"key": "protocol"},
                    {"text": ":"},
                    {"key": "dst-port"},
                ],
            ],
            only=[{"key": "action", "value": "dst-nat"}],
        )

        # uniq-id is kept for the unique id migration and as an attribute;
        # entities are keyed on the stable .id, so duplicates are fine.
        for uid in self.ds["nat"]:
            self.ds["nat"][uid]["comment"] = str(self.ds["nat"][uid]["comment"])

    # ---------------------------
    #   get_mangle
    # ---------------------------
    def get_mangle(self) -> None:
        """Get Mangle data from Mikrotik"""
        self.ds["mangle"] = parse_api(
            data=self.ds["mangle"],
            prune=True,
            source=self.api.query("/ip/firewall/mangle"),
            key=".id",
            vals=[
                {"name": ".id"},
                {"name": "chain"},
                {"name": "action"},
                {"name": "comment"},
                {"name": "address-list"},
                {"name": "passthrough", "type": "bool", "default": False},
                {"name": "protocol", "default": "any"},
                {"name": "src-address", "default": "any"},
                {"name": "src-port", "default": "any"},
                {"name": "dst-address", "default": "any"},
                {"name": "dst-port", "default": "any"},
                {"name": "src-address-list", "default": "any"},
                {"name": "dst-address-list", "default": "any"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            val_proc=[
                [
                    {"name": "uniq-id"},
                    {"action": "combine"},
                    {"key": "chain"},
                    {"text": ","},
                    {"key": "action"},
                    {"text": ","},
                    {"key": "protocol"},
                    {"text": ","},
                    {"key": "src-address"},
                    {"text": ":"},
                    {"key": "src-port"},
                    {"text": "-"},
                    {"key": "dst-address"},
                    {"text": ":"},
                    {"key": "dst-port"},
                    {"text": ","},
                    {"key": "src-address-list"},
                    {"text": "-"},
                    {"key": "dst-address-list"},
                ],
                [
                    {"name": "name"},
                    {"action": "combine"},
                    {"key": "action"},
                    {"text": ","},
                    {"key": "protocol"},
                    {"text": ":"},
                    {"key": "dst-port"},
                ],
            ],
            skip=[
                {"name": "dynamic", "value": True},
                {"name": "action", "value": "jump"},
            ],
        )

        # uniq-id is kept for the unique id migration and as an attribute;
        # entities are keyed on the stable .id, so duplicates are fine.
        for uid in self.ds["mangle"]:
            self.ds["mangle"][uid]["comment"] = str(self.ds["mangle"][uid]["comment"])

    # ---------------------------
    #   get_filter
    # ---------------------------
    def get_filter(self) -> None:
        """Get Filter data from Mikrotik"""
        self.ds["filter"] = parse_api(
            data=self.ds["filter"],
            prune=True,
            source=self.api.query("/ip/firewall/filter"),
            key=".id",
            vals=[
                {"name": ".id"},
                {"name": "chain"},
                {"name": "action"},
                {"name": "comment"},
                {"name": "address-list"},
                {"name": "protocol", "default": "any"},
                {"name": "in-interface", "default": "any"},
                {"name": "in-interface-list", "default": "any"},
                {"name": "out-interface", "default": "any"},
                {"name": "out-interface-list", "default": "any"},
                {"name": "src-address", "default": "any"},
                {"name": "src-address-list", "default": "any"},
                {"name": "src-port", "default": "any"},
                {"name": "dst-address", "default": "any"},
                {"name": "dst-address-list", "default": "any"},
                {"name": "dst-port", "default": "any"},
                {"name": "layer7-protocol", "default": "any"},
                {"name": "connection-state", "default": "any"},
                {"name": "tcp-flags", "default": "any"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                    "default": True,
                },
            ],
            val_proc=[
                [
                    {"name": "uniq-id"},
                    {"action": "combine"},
                    {"key": "chain"},
                    {"text": ","},
                    {"key": "action"},
                    {"text": ","},
                    {"key": "protocol"},
                    {"text": ","},
                    {"key": "layer7-protocol"},
                    {"text": ","},
                    {"key": "in-interface"},
                    {"text": ","},
                    {"key": "in-interface-list"},
                    {"text": ":"},
                    {"key": "src-address"},
                    {"text": ","},
                    {"key": "src-address-list"},
                    {"text": ":"},
                    {"key": "src-port"},
                    {"text": "-"},
                    {"key": "out-interface"},
                    {"text": ","},
                    {"key": "out-interface-list"},
                    {"text": ":"},
                    {"key": "dst-address"},
                    {"text": ","},
                    {"key": "dst-address-list"},
                    {"text": ":"},
                    {"key": "dst-port"},
                ],
                [
                    {"name": "name"},
                    {"action": "combine"},
                    {"key": "action"},
                    {"text": ","},
                    {"key": "protocol"},
                    {"text": ":"},
                    {"key": "dst-port"},
                ],
            ],
            skip=[
                {"name": "dynamic", "value": True},
                {"name": "action", "value": "jump"},
            ],
        )

        # uniq-id is kept for the unique id migration and as an attribute;
        # entities are keyed on the stable .id, so duplicates are fine.
        for uid in self.ds["filter"]:
            self.ds["filter"][uid]["comment"] = str(self.ds["filter"][uid]["comment"])

    # ---------------------------
    #   get_kidcontrol
    # ---------------------------
    def get_kidcontrol(self) -> None:
        """Get Kid-control data from Mikrotik"""
        self.ds["kid-control"] = parse_api(
            data=self.ds["kid-control"],
            prune=True,
            source=self.api.query("/ip/kid-control"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "rate-limit"},
                {"name": "mon", "default": "None"},
                {"name": "tue", "default": "None"},
                {"name": "wed", "default": "None"},
                {"name": "thu", "default": "None"},
                {"name": "fri", "default": "None"},
                {"name": "sat", "default": "None"},
                {"name": "sun", "default": "None"},
                {"name": "comment"},
                {"name": "blocked", "type": "bool", "default": False},
                {"name": "paused", "type": "bool", "reverse": True},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
        )

        for uid in self.ds["kid-control"]:
            self.ds["kid-control"][uid]["comment"] = str(
                self.ds["kid-control"][uid]["comment"]
            )

    # ---------------------------
    #   get_ppp
    # ---------------------------
    def get_ppp(self) -> None:
        """Get PPP data from Mikrotik"""
        self.ds["ppp_secret"] = parse_api(
            data=self.ds["ppp_secret"],
            prune=True,
            source=self.api.query("/ppp/secret"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "service"},
                {"name": "profile"},
                {"name": "comment"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            ensure_vals=[
                {"name": "caller-id", "default": ""},
                {"name": "address", "default": ""},
                {"name": "encoding", "default": ""},
                {"name": "connected", "default": False},
            ],
        )

        self.ds["ppp_active"] = parse_api(
            data={},
            source=self.api.query("/ppp/active"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "service"},
                {"name": "caller-id"},
                {"name": "address"},
                {"name": "encoding"},
            ],
        )

        for uid in self.ds["ppp_secret"]:
            self.ds["ppp_secret"][uid]["comment"] = str(
                self.ds["ppp_secret"][uid]["comment"]
            )

            if self.ds["ppp_secret"][uid]["name"] in self.ds["ppp_active"]:
                self.ds["ppp_secret"][uid]["connected"] = True
                self.ds["ppp_secret"][uid]["caller-id"] = self.ds["ppp_active"][uid][
                    "caller-id"
                ]
                self.ds["ppp_secret"][uid]["address"] = self.ds["ppp_active"][uid][
                    "address"
                ]
                self.ds["ppp_secret"][uid]["encoding"] = self.ds["ppp_active"][uid][
                    "encoding"
                ]
            else:
                self.ds["ppp_secret"][uid]["connected"] = False
                self.ds["ppp_secret"][uid]["caller-id"] = "not connected"
                self.ds["ppp_secret"][uid]["address"] = "not connected"
                self.ds["ppp_secret"][uid]["encoding"] = "not connected"

    # ---------------------------
    #   get_netwatch
    # ---------------------------
    def get_netwatch(self) -> None:
        """Get netwatch data from Mikrotik"""
        self.ds["netwatch"] = parse_api(
            data=self.ds["netwatch"],
            prune=True,
            source=self.api.query("/tool/netwatch"),
            key="host",
            vals=[
                {"name": "host"},
                {"name": "type"},
                {"name": "interval"},
                {"name": "port"},
                {"name": "http-codes"},
                {"name": "status", "type": "bool", "default": None},
                {"name": "comment"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
        )

    # ---------------------------
    #   get_system_routerboard
    # ---------------------------
    def get_system_routerboard(self) -> None:
        """Get routerboard data from Mikrotik"""
        if self.ds["resource"]["board-name"].startswith("x86") or self.ds["resource"][
            "board-name"
        ].startswith("CHR"):
            self.ds["routerboard"]["routerboard"] = False
            self.ds["routerboard"]["model"] = self.ds["resource"]["board-name"]
            self.ds["routerboard"]["serial-number"] = "N/A"
        else:
            self.ds["routerboard"] = parse_api(
                data=self.ds["routerboard"],
                source=self.api.query("/system/routerboard"),
                vals=[
                    {"name": "routerboard", "type": "bool"},
                    {"name": "model", "default": "unknown"},
                    {"name": "serial-number", "default": "unknown"},
                    {"name": "current-firmware", "default": "unknown"},
                    {"name": "upgrade-firmware", "default": "unknown"},
                ],
            )

            if (
                "write" not in self.ds["access"]
                or "policy" not in self.ds["access"]
                or "reboot" not in self.ds["access"]
            ):
                self.ds["routerboard"].pop("current-firmware")
                self.ds["routerboard"].pop("upgrade-firmware")

    # ---------------------------
    #   get_system_health
    # ---------------------------
    def get_system_health(self) -> None:
        """Get routerboard data from Mikrotik"""
        if (
            "write" not in self.ds["access"]
            or "policy" not in self.ds["access"]
            or "reboot" not in self.ds["access"]
        ):
            return

        if 0 < self.major_fw_version < 7:
            self.ds["health"] = parse_api(
                data=self.ds["health"],
                source=self.api.query("/system/health"),
                vals=[
                    {"name": "temperature", "default": 0},
                    {"name": "voltage", "default": 0},
                    {"name": "cpu-temperature", "default": 0},
                    {"name": "power-consumption", "default": 0},
                    {"name": "board-temperature1", "default": 0},
                    {"name": "phy-temperature", "default": 0},
                    {"name": "fan1-speed", "default": 0},
                    {"name": "fan2-speed", "default": 0},
                ],
            )
        elif 0 < self.major_fw_version >= 7:
            self.ds["health7"] = parse_api(
                data=self.ds["health7"],
                source=self.api.query("/system/health"),
                key="name",
                vals=[
                    {"name": "value", "default": "unknown"},
                ],
            )
            if self.ds["health7"]:
                for uid, vals in self.ds["health7"].items():
                    self.ds["health"][uid] = vals["value"]

        for key in self.ds["health"]:
            if "temperature" in key:
                self._apply_hysteresis("health", key)

    # ---------------------------
    #   _apply_hysteresis
    # ---------------------------
    def _apply_hysteresis(self, path, key) -> None:
        """Hold a jittery value until it moves enough or gets old.

        CPU load and temperatures flicker by a point every poll, and each
        change is a recorder row. Publish a new value only when it differs
        by HYSTERESIS_DELTA or the last one is HYSTERESIS_MAX_AGE old.
        """
        value = self.ds[path].get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            self._published.pop((path, key), None)
            return

        now = monotonic()
        published = self._published.get((path, key))
        if published is not None:
            old_value, old_number, since = published
            if (
                abs(number - old_number) < HYSTERESIS_DELTA
                and now - since < HYSTERESIS_MAX_AGE
            ):
                self.ds[path][key] = old_value
                return

        self._published[(path, key)] = (value, number, now)

    # ---------------------------
    #   get_system_resource
    # ---------------------------
    def get_system_resource(self) -> None:
        """Get system resources data from Mikrotik"""
        self.ds["resource"] = parse_api(
            data=self.ds["resource"],
            source=self.api.query("/system/resource"),
            vals=[
                {"name": "platform", "default": "unknown"},
                {"name": "board-name", "default": "unknown"},
                {"name": "architecture-name", "default": "unknown"},
                {"name": "version", "default": "unknown"},
                {"name": "uptime_str", "source": "uptime", "default": "unknown"},
                {"name": "cpu-load", "default": "unknown"},
                {"name": "free-memory", "default": 0},
                {"name": "total-memory", "default": 0},
                {"name": "free-hdd-space", "default": 0},
                {"name": "total-hdd-space", "default": 0},
            ],
            ensure_vals=[
                {"name": "uptime", "default": 0},
                {"name": "uptime_epoch", "default": 0},
                {"name": "clients_wired", "default": 0},
                {"name": "clients_wireless", "default": 0},
                {"name": "captive_authorized", "default": 0},
            ],
        )

        self._apply_hysteresis("resource", "cpu-load")

        full_version = self.ds["resource"].get("version")
        if full_version and full_version != "unknown":
            try:
                # RouterOS may report "7.23 (stable)", "7.23.1", etc.
                self.major_fw_version, self.minor_fw_version = (
                    parse_routeros_major_minor(full_version)
                )
                _LOGGER.debug(
                    "Mikrotik %s FW version major=%s minor=%s (%s)",
                    self.host,
                    self.major_fw_version,
                    self.minor_fw_version,
                    full_version,
                )
            except Exception as err:
                _LOGGER.error(
                    "Mikrotik %s unable to determine major/minor FW version (%s): %s",
                    self.host,
                    full_version,
                    err,
                )

        tmp_uptime = 0
        tmp = re.split(r"(\d+)[s]", self.ds["resource"]["uptime_str"])
        if len(tmp) > 1:
            tmp_uptime += int(tmp[1])
        tmp = re.split(r"(\d+)[m]", self.ds["resource"]["uptime_str"])
        if len(tmp) > 1:
            tmp_uptime += int(tmp[1]) * 60
        tmp = re.split(r"(\d+)[h]", self.ds["resource"]["uptime_str"])
        if len(tmp) > 1:
            tmp_uptime += int(tmp[1]) * 3600
        tmp = re.split(r"(\d+)[d]", self.ds["resource"]["uptime_str"])
        if len(tmp) > 1:
            tmp_uptime += int(tmp[1]) * 86400
        tmp = re.split(r"(\d+)[w]", self.ds["resource"]["uptime_str"])
        if len(tmp) > 1:
            tmp_uptime += int(tmp[1]) * 604800

        self.ds["resource"]["uptime_epoch"] = tmp_uptime
        now = datetime.now().replace(microsecond=0)
        uptime_tm = datetime.timestamp(now - timedelta(seconds=tmp_uptime))
        update_uptime = False
        if not self.ds["resource"]["uptime"]:
            update_uptime = True
        else:
            uptime_old = datetime.timestamp(self.ds["resource"]["uptime"])
            if uptime_tm > uptime_old + 10:
                update_uptime = True

        if update_uptime:
            self.ds["resource"]["uptime"] = utc_from_timestamp(uptime_tm)

        if self.ds["resource"]["total-memory"] > 0:
            self.ds["resource"]["memory-usage"] = round(
                (
                    (
                        self.ds["resource"]["total-memory"]
                        - self.ds["resource"]["free-memory"]
                    )
                    / self.ds["resource"]["total-memory"]
                )
                * 100
            )
        else:
            self.ds["resource"]["memory-usage"] = "unknown"

        if self.ds["resource"]["total-hdd-space"] > 0:
            self.ds["resource"]["hdd-usage"] = round(
                (
                    (
                        self.ds["resource"]["total-hdd-space"]
                        - self.ds["resource"]["free-hdd-space"]
                    )
                    / self.ds["resource"]["total-hdd-space"]
                )
                * 100
            )
        else:
            self.ds["resource"]["hdd-usage"] = "unknown"

        if (
            "uptime_epoch" in self.ds["resource"]
            and self.rebootcheck > self.ds["resource"]["uptime_epoch"]
        ):
            # Rebooted, possibly into a new version: check again next poll
            self._last_firmware_check = None
            self.force_full_refresh = True

        if "uptime_epoch" in self.ds["resource"]:
            self.rebootcheck = self.ds["resource"]["uptime_epoch"]

    # ---------------------------
    #   get_firmware_update
    # ---------------------------
    def get_firmware_update(self) -> None:
        """Check for firmware update on Mikrotik"""
        if (
            "write" not in self.ds["access"]
            or "policy" not in self.ds["access"]
            or "reboot" not in self.ds["access"]
        ):
            return

        # check-for-updates is a command; without firmware changes only read
        # the status of the router's own last check.
        if CHANGE_FIRMWARE in self.option_allowed_changes:
            self.execute(
                "/system/package/update",
                "check-for-updates",
                None,
                None,
                {"duration": 10},
            )
        self.ds["fw-update"] = parse_api(
            data={},
            source=self.api.query("/system/package/update"),
            vals=[
                {"name": "status"},
                {"name": "channel", "default": "unknown"},
                {"name": "installed-version", "default": None},
                {"name": "latest-version", "default": None},
            ],
        )

        self.ds["fw-update"]["available"] = (
            self.ds["fw-update"].get("status") == "New version is available"
            and self.ds["fw-update"].get("installed-version")
            not in (None, "", "unknown")
            and self.ds["fw-update"].get("latest-version") not in (None, "", "unknown")
        )

    # ---------------------------
    #   get_ups
    # ---------------------------
    def get_ups(self) -> None:
        """Get UPS info from Mikrotik"""
        self.ds["ups"] = parse_api(
            data=self.ds["ups"],
            source=self.api.query("/system/ups"),
            vals=[
                {"name": "name", "default": "unknown"},
                {"name": "offline-time", "default": "unknown"},
                {"name": "min-runtime", "default": "unknown"},
                {"name": "alarm-setting", "default": "unknown"},
                {"name": "model", "default": "unknown"},
                {"name": "serial", "default": "unknown"},
                {"name": "manufacture-date", "default": "unknown"},
                {"name": "nominal-battery-voltage", "default": "unknown"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            ensure_vals=[
                {"name": "on-line", "type": "bool"},
                {"name": "runtime-left", "default": "unknown"},
                {"name": "battery-charge", "default": 0},
                {"name": "battery-voltage", "default": 0.0},
                {"name": "line-voltage", "default": 0},
                {"name": "load", "default": 0},
                {"name": "hid-self-test", "default": "unknown"},
            ],
        )
        if self.ds["ups"]["enabled"]:
            self.ds["ups"] = parse_api(
                data=self.ds["ups"],
                source=self.api.query(
                    "/system/ups",
                    command="monitor",
                    args={".id": 0, "once": True},
                ),
                vals=[
                    {"name": "on-line", "type": "bool"},
                    {"name": "runtime-left", "default": 0},
                    {"name": "battery-charge", "default": 0},
                    {"name": "battery-voltage", "default": 0.0},
                    {"name": "line-voltage", "default": 0},
                    {"name": "load", "default": 0},
                    {"name": "hid-self-test", "default": "unknown"},
                ],
            )

    # ---------------------------
    #   get_gps
    # ---------------------------
    def get_gps(self) -> None:
        """Get GPS data from Mikrotik"""
        self.ds["gps"] = parse_api(
            data=self.ds["gps"],
            source=self.api.query(
                "/system/gps",
                command="monitor",
                args={"once": True},
            ),
            vals=[
                {"name": "valid", "type": "bool"},
                {"name": "latitude", "default": "unknown"},
                {"name": "longitude", "default": "unknown"},
                {"name": "altitude", "default": "unknown"},
                {"name": "speed", "default": "unknown"},
                {"name": "destination-bearing", "default": "unknown"},
                {"name": "true-bearing", "default": "unknown"},
                {"name": "magnetic-bearing", "default": "unknown"},
                {"name": "satellites", "default": 0},
                {"name": "fix-quality", "default": 0},
                {"name": "horizontal-dilution", "default": "unknown"},
            ],
        )
        if self.ds["gps"]["latitude"] == "none":
            self.ds["gps"]["latitude"] = "unavailable"
        if self.ds["gps"]["longitude"] == "none":
            self.ds["gps"]["longitude"] = "unavailable"

    # ---------------------------
    #   get_script
    # ---------------------------
    def get_script(self) -> None:
        """Get list of all scripts from Mikrotik"""
        self.ds["script"] = parse_api(
            data=self.ds["script"],
            prune=True,
            source=self.api.query("/system/script"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "last-started", "default": "unknown"},
                {"name": "run-count", "default": "unknown"},
            ],
        )

    # ---------------------------
    #   get_environment
    # ---------------------------
    def get_environment(self) -> None:
        """Get list of all environment variables from Mikrotik"""
        self.ds["environment"] = parse_api(
            data=self.ds["environment"],
            prune=True,
            source=self.api.query("/system/script/environment"),
            key="name",
            # Scripts often keep tokens in globals
            redact={"value"},
            vals=[
                {"name": "name"},
                {"name": "value"},
            ],
        )

    # ---------------------------
    #   get_captive
    # ---------------------------
    def get_captive(self) -> None:
        """Get captive portal hosts from Mikrotik."""
        self.ds["hostspot_host"] = parse_api(
            data={},
            source=self.api.query("/ip/hotspot/host", ignore_trap=True),
            key="mac-address",
            vals=[
                {"name": "mac-address"},
                {"name": "authorized", "type": "bool"},
                {"name": "bypassed", "type": "bool"},
            ],
        )

        auth_hosts = sum(
            1
            for uid in self.ds["hostspot_host"]
            if self.ds["hostspot_host"][uid]["authorized"]
        )
        self.ds["resource"]["captive_authorized"] = auth_hosts

    # ---------------------------
    #   get_queue
    # ---------------------------
    def get_queue(self) -> None:
        """Get Queue data from Mikrotik"""
        self.ds["queue"] = parse_api(
            data=self.ds["queue"],
            prune=True,
            source=self.api.query("/queue/simple"),
            key="name",
            vals=[
                {"name": ".id"},
                {"name": "name", "default": "unknown"},
                {"name": "target", "default": "unknown"},
                {"name": "rate", "default": "0/0"},
                {"name": "max-limit", "default": "0/0"},
                {"name": "limit-at", "default": "0/0"},
                {"name": "burst-limit", "default": "0/0"},
                {"name": "burst-threshold", "default": "0/0"},
                {"name": "burst-time", "default": "0s/0s"},
                {"name": "packet-marks", "default": "none"},
                {"name": "parent", "default": "none"},
                {"name": "comment"},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
        )

        for uid, vals in self.ds["queue"].items():
            self.ds["queue"][uid]["comment"] = str(self.ds["queue"][uid]["comment"])

            for source in (
                "max-limit",
                "rate",
                "limit-at",
                "burst-limit",
                "burst-threshold",
            ):
                upload, download = _split_pair(vals[source], int)
                vals[f"upload-{source}"] = f"{upload} bps"
                vals[f"download-{source}"] = f"{download} bps"

            upload, download = _split_pair(vals["burst-time"], str, "0s")
            vals["upload-burst-time"] = upload
            vals["download-burst-time"] = download

    # ---------------------------
    #   get_arp
    # ---------------------------
    def get_arp(self) -> None:
        """Get ARP data from Mikrotik"""
        # Built aside and swapped in at the end (item 3)
        arp = parse_api(
            data=dict(self.ds["arp"]),
            prune=True,
            source=self.api.query("/ip/arp"),
            key="mac-address",
            vals=[{"name": "mac-address"}, {"name": "address"}, {"name": "interface"}],
            ensure_vals=[{"name": "bridge", "default": ""}],
        )

        for uid, vals in arp.items():
            if vals["interface"] in self.ds["bridge"] and uid in self.ds["bridge_host"]:
                arp[uid]["bridge"] = vals["interface"]
                arp[uid]["interface"] = self.ds["bridge_host"][uid]["interface"]

        if self.ds["dhcp-client"]:
            to_remove = [
                uid
                for uid, vals in arp.items()
                if vals["interface"] in self.ds["dhcp-client"]
            ]

            for uid in to_remove:
                arp.pop(uid)

        self.ds["arp"] = arp

    # ---------------------------
    #   get_dns
    # ---------------------------
    def get_dns(self) -> None:
        """Get static DNS data from Mikrotik"""
        self.ds["dns"] = parse_api(
            data=self.ds["dns"],
            prune=True,
            source=self.api.query("/ip/dns/static"),
            key="name",
            vals=[{"name": "name"}, {"name": "address"}, {"name": "comment"}],
        )

        for uid, vals in self.ds["dns"].items():
            self.ds["dns"][uid]["comment"] = str(self.ds["dns"][uid]["comment"])

    # ---------------------------
    #   get_dhcp
    # ---------------------------
    def get_dhcp(self) -> None:
        """Get DHCP data from Mikrotik"""
        self.ds["dhcp"] = parse_api(
            data=dict(self.ds["dhcp"]),
            prune=True,
            source=self.api.query("/ip/dhcp-server/lease"),
            key="mac-address",
            vals=[
                {"name": "mac-address"},
                {"name": "active-mac-address", "default": "unknown"},
                {"name": "address", "default": "unknown"},
                {"name": "active-address", "default": "unknown"},
                {"name": "host-name", "default": "unknown"},
                {"name": "status", "default": "unknown"},
                {"name": "last-seen", "default": "unknown"},
                {"name": "server", "default": "unknown"},
                {"name": "comment", "default": ""},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
            ensure_vals=[{"name": "interface", "default": "unknown"}],
        )

        dhcpserver_query = False
        for uid in self.ds["dhcp"]:
            self.ds["dhcp"][uid]["comment"] = str(self.ds["dhcp"][uid]["comment"])

            # is_valid_ip
            if self.ds["dhcp"][uid]["address"] != "unknown":
                if not is_valid_ip(self.ds["dhcp"][uid]["address"]):
                    self.ds["dhcp"][uid]["address"] = "unknown"

                if self.ds["dhcp"][uid]["active-address"] not in [
                    self.ds["dhcp"][uid]["address"],
                    "unknown",
                ]:
                    self.ds["dhcp"][uid]["address"] = self.ds["dhcp"][uid][
                        "active-address"
                    ]

                if (
                    self.ds["dhcp"][uid]["mac-address"]
                    != self.ds["dhcp"][uid]["active-mac-address"]
                    != "unknown"
                ):
                    self.ds["dhcp"][uid]["mac-address"] = self.ds["dhcp"][uid][
                        "active-mac-address"
                    ]

            # Static leases use "all"; only look for a new server name once
            # per slow poll instead of on every poll.
            server = self.ds["dhcp"][uid]["server"]
            if (
                not dhcpserver_query
                and server not in self.ds["dhcp-server"]
                and server not in self._dhcp_server_misses
            ):
                self.get_dhcp_server()
                dhcpserver_query = True
                if server not in self.ds["dhcp-server"]:
                    self._dhcp_server_misses.add(server)

            if self.ds["dhcp"][uid]["server"] in self.ds["dhcp-server"]:
                self.ds["dhcp"][uid]["interface"] = self.ds["dhcp-server"][
                    self.ds["dhcp"][uid]["server"]
                ]["interface"]
            elif uid in self.ds["arp"]:
                if self.ds["arp"][uid]["bridge"]:
                    self.ds["dhcp"][uid]["interface"] = self.ds["arp"][uid]["bridge"]
                else:
                    self.ds["dhcp"][uid]["interface"] = self.ds["arp"][uid]["interface"]

    # ---------------------------
    #   get_dhcp_server
    # ---------------------------
    def get_dhcp_server(self) -> None:
        """Get DHCP server data from Mikrotik"""
        self.ds["dhcp-server"] = parse_api(
            data=self.ds["dhcp-server"],
            prune=True,
            source=self.api.query("/ip/dhcp-server"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "interface", "default": "unknown"},
            ],
        )

    # ---------------------------
    #   get_dhcp_client
    # ---------------------------
    def get_dhcp_client(self) -> None:
        """Get DHCP client data from Mikrotik"""
        self.ds["dhcp-client"] = parse_api(
            data=self.ds["dhcp-client"],
            prune=True,
            source=self.api.query("/ip/dhcp-client"),
            key="interface",
            vals=[
                {"name": "interface", "default": "unknown"},
                {"name": "status", "default": "unknown"},
                {"name": "address", "default": "unknown"},
            ],
        )

    # ---------------------------
    #   get_dhcp_network
    # ---------------------------
    def get_dhcp_network(self) -> None:
        """Get DHCP network data from Mikrotik"""
        self.ds["dhcp-network"] = parse_api(
            data=self.ds["dhcp-network"],
            prune=True,
            source=self.api.query("/ip/dhcp-server/network"),
            key="address",
            vals=[
                {"name": "address"},
                {"name": "gateway", "default": ""},
                {"name": "netmask", "default": ""},
                {"name": "dns-server", "default": ""},
                {"name": "domain", "default": ""},
            ],
            ensure_vals=[{"name": "address"}, {"name": "IPv4Network", "default": ""}],
        )

        for uid, vals in self.ds["dhcp-network"].items():
            if vals["IPv4Network"] == "":
                try:
                    self.ds["dhcp-network"][uid]["IPv4Network"] = IPv4Network(
                        vals["address"], strict=False
                    )
                except ValueError:
                    self.ds["dhcp-network"][uid]["IPv4Network"] = None

    # ---------------------------
    #   get_capsman_hosts
    # ---------------------------
    def get_capsman_hosts(self) -> None:
        """Get CAPS-MAN hosts data from Mikrotik"""
        self.ds["capsman_hosts"] = parse_api(
            data={},
            source=self.api.query("/caps-man/registration-table", ignore_trap=True),
            key="mac-address",
            vals=[
                {"name": "mac-address"},
                {"name": "interface", "default": "unknown"},
                {"name": "ssid", "default": "unknown"},
            ],
        )

    # ---------------------------
    #   get_wireless
    # ---------------------------
    def get_wireless(self) -> None:
        """Get wireless data from Mikrotik"""
        self.ds["wireless"] = {}
        for wifimodule in self._wifimodules:
            # The table starts empty each poll; pruning here would drop the
            # interfaces of the previous wifi module.
            self.ds["wireless"] = parse_api(
                data=self.ds["wireless"],
                source=self.api.query(f"/interface/{wifimodule}", ignore_trap=True),
                key="name",
                vals=[
                    {"name": "master-interface", "default": ""},
                    {"name": "mac-address", "default": "unknown"},
                    {"name": "ssid", "default": "unknown"},
                    {"name": "mode", "default": "unknown"},
                    {"name": "radio-name", "default": "unknown"},
                    {"name": "interface-type", "default": "unknown"},
                    {"name": "country", "default": "unknown"},
                    {"name": "installation", "default": "unknown"},
                    {"name": "antenna-gain", "default": "unknown"},
                    {"name": "frequency", "default": "unknown"},
                    {"name": "band", "default": "unknown"},
                    {"name": "channel-width", "default": "unknown"},
                    {"name": "secondary-frequency", "default": "unknown"},
                    {"name": "wireless-protocol", "default": "unknown"},
                    {"name": "rate-set", "default": "unknown"},
                    {"name": "distance", "default": "unknown"},
                    {"name": "tx-power-mode", "default": "unknown"},
                    {"name": "vlan-id", "default": "unknown"},
                    {"name": "wds-mode", "default": "unknown"},
                    {"name": "wds-default-bridge", "default": "unknown"},
                    {"name": "bridge-mode", "default": "unknown"},
                    {"name": "hide-ssid", "type": "bool"},
                    {"name": "running", "type": "bool"},
                    {"name": "disabled", "type": "bool"},
                ],
            )

        for uid in self.ds["wireless"]:
            master = self.ds["wireless"].get(
                self.ds["wireless"][uid]["master-interface"]
            )
            if master:
                for tmp in self.ds["wireless"][uid]:
                    if self.ds["wireless"][uid][tmp] == "unknown":
                        self.ds["wireless"][uid][tmp] = master.get(tmp, "unknown")

            if uid in self.ds["interface"]:
                for tmp in self.ds["wireless"][uid]:
                    self.ds["interface"][uid][tmp] = self.ds["wireless"][uid][tmp]

    # ---------------------------
    #   get_wireless_hosts
    # ---------------------------
    def get_wireless_hosts(self) -> None:
        """Get wireless hosts data from Mikrotik"""
        # Build a new table and swap it in, so readers never see it half-filled
        wireless_hosts = {}
        for wifimodule in self._wifimodules:
            # The wifi/wifiwave2 registration table reports RSSI as "signal"
            signal_source = (
                "signal" if wifimodule in ("wifi", "wifiwave2") else "signal-strength"
            )
            wireless_hosts = parse_api(
                data=wireless_hosts,
                source=self.api.query(
                    f"/interface/{wifimodule}/registration-table",
                    ignore_trap=True,
                ),
                key="mac-address",
                vals=[
                    {"name": "mac-address"},
                    {"name": "interface", "default": "unknown"},
                    {"name": "ap", "type": "bool"},
                    {"name": "uptime"},
                    {"name": "signal-strength", "source": signal_source},
                    {"name": "tx-ccq"},
                    {"name": "tx-rate"},
                    {"name": "rx-rate"},
                ],
            )
        self.ds["wireless_hosts"] = wireless_hosts

    # ---------------------------
    #   async_process_host
    # ---------------------------
    async def async_process_host(self) -> None:
        """Get host tracking data"""
        # Both coordinators call this, and it awaits the vendor lookup
        async with self._host_lock:
            await self._async_process_host()

    async def _async_process_host(self) -> None:
        """Merge the client tables into the host table"""
        # Add hosts from CAPS-MAN
        capsman_detected = {}
        if self.support_capsman:
            for uid, vals in self.ds["capsman_hosts"].items():
                if uid not in self.ds["host"]:
                    self.ds["host"][uid] = {}

                capsman_detected[uid] = True
                self.ds["host"][uid]["source"] = "capsman"
                self.ds["host"][uid]["available"] = True
                self.ds["host"][uid]["last-seen"] = utcnow()
                for key in ["mac-address", "interface"]:
                    self.ds["host"][uid][key] = vals[key]

        # Add hosts from wireless
        wireless_detected = {}
        wireless_signal_attributes = [
            "signal-strength",
            "tx-ccq",
            "tx-rate",
            "rx-rate",
        ]
        if self.support_wireless:
            for uid, vals in self.ds["wireless_hosts"].items():
                if vals["ap"]:
                    continue

                if uid in capsman_detected:
                    continue

                if uid not in self.ds["host"]:
                    self.ds["host"][uid] = {}

                wireless_detected[uid] = True
                self.ds["host"][uid]["source"] = "wireless"
                self.ds["host"][uid]["available"] = True
                self.ds["host"][uid]["last-seen"] = utcnow()
                for key in [
                    "mac-address",
                    "interface",
                    *wireless_signal_attributes,
                ]:
                    self.ds["host"][uid][key] = vals[key]

        # Add hosts from DHCP
        dhcp_detected = {}
        for uid, vals in self.ds["dhcp"].items():
            if not vals["enabled"]:
                continue

            if uid in capsman_detected or uid in wireless_detected:
                continue

            if uid not in self.ds["host"]:
                self.ds["host"][uid] = {}

            dhcp_detected[uid] = True
            self.ds["host"][uid]["source"] = "dhcp"
            for key in ["address", "mac-address", "interface"]:
                self.ds["host"][uid][key] = vals[key]

        # Add hosts from ARP
        for uid, vals in self.ds["arp"].items():
            if (
                uid in capsman_detected
                or uid in wireless_detected
                or uid in dhcp_detected
            ):
                continue

            if uid not in self.ds["host"]:
                self.ds["host"][uid] = {}

            self.ds["host"][uid]["source"] = "arp"
            for key in ["address", "mac-address", "interface"]:
                self.ds["host"][uid][key] = vals[key]

        # Add restored hosts from hass registry
        if not self.host_hass_recovered:
            self.host_hass_recovered = True
            for uid in self.ds["host_hass"]:
                if uid not in self.ds["host"]:
                    self.ds["host"][uid] = {"source": "restored"}
                    self.ds["host"][uid]["mac-address"] = uid
                    self.ds["host"][uid]["host-name"] = self.ds["host_hass"][uid]

        for uid, vals in self.ds["host"].items():
            # Add missing default values
            for key, default in zip(
                [
                    "address",
                    "mac-address",
                    "interface",
                    "host-name",
                    "manufacturer",
                    "last-seen",
                    "available",
                ],
                ["unknown", "unknown", "unknown", "unknown", "detect", False, False],
            ):
                if key not in self.ds["host"][uid]:
                    self.ds["host"][uid][key] = default

        # Without the "test" policy nothing pings wired hosts. A host in the
        # bridge host table (learned on a port, aged out by the router) is
        # then the evidence it is present; a host without any evidence
        # source is unknown instead of permanently away.
        can_ping = "test" in self.ds["access"]
        now = utcnow()
        for uid, vals in self.ds["host"].items():
            if vals["source"] in ("capsman", "wireless"):
                vals["presence-tracked"] = True
                continue
            bridged = self.ds["bridge_host"].get(uid, {}).get("enabled", False)
            if not can_ping and bridged:
                vals["available"] = True
                vals["last-seen"] = now
            vals["presence-tracked"] = can_ping or bool(bridged)

        # Process hosts
        self.ds["resource"]["clients_wired"] = 0
        self.ds["resource"]["clients_wireless"] = 0
        for uid, vals in self.ds["host"].items():
            if vals["source"] != "wireless":
                for key in wireless_signal_attributes:
                    self.ds["host"][uid].pop(key, None)

            # Captive portal data
            if self.option_sensor_client_captive:
                if uid in self.ds["hostspot_host"]:
                    self.ds["host"][uid]["authorized"] = self.ds["hostspot_host"][uid][
                        "authorized"
                    ]
                    self.ds["host"][uid]["bypassed"] = self.ds["hostspot_host"][uid][
                        "bypassed"
                    ]
                elif "authorized" in self.ds["host"][uid]:
                    del self.ds["host"][uid]["authorized"]
                    del self.ds["host"][uid]["bypassed"]

            # CAPS-MAN availability
            if vals["source"] == "capsman" and uid not in capsman_detected:
                self.ds["host"][uid]["available"] = False

            # Wireless availability
            if vals["source"] == "wireless" and uid not in wireless_detected:
                self.ds["host"][uid]["available"] = False

            # Update IP and interface (DHCP/returned host)
            if (
                uid in self.ds["dhcp"]
                and self.ds["dhcp"][uid]["enabled"]
                and "." in self.ds["dhcp"][uid]["address"]
            ):
                if self.ds["dhcp"][uid]["address"] != self.ds["host"][uid]["address"]:
                    self.ds["host"][uid]["address"] = self.ds["dhcp"][uid]["address"]
                    if vals["source"] not in ["capsman", "wireless"]:
                        self.ds["host"][uid]["source"] = "dhcp"
                        self.ds["host"][uid]["interface"] = self.ds["dhcp"][uid][
                            "interface"
                        ]

            elif (
                uid in self.ds["arp"]
                and "." in self.ds["arp"][uid]["address"]
                and self.ds["arp"][uid]["address"] != self.ds["host"][uid]["address"]
            ):
                self.ds["host"][uid]["address"] = self.ds["arp"][uid]["address"]
                if vals["source"] not in ["capsman", "wireless"]:
                    self.ds["host"][uid]["source"] = "arp"
                    self.ds["host"][uid]["interface"] = self.ds["arp"][uid]["interface"]

            if vals["host-name"] == "unknown":
                # Resolve hostname from static DNS
                if vals["address"] != "unknown":
                    for dns_uid, dns_vals in self.ds["dns"].items():
                        if dns_vals["address"] == vals["address"]:
                            if dns_vals["comment"].split("#", 1)[0] != "":
                                self.ds["host"][uid]["host-name"] = dns_vals[
                                    "comment"
                                ].split("#", 1)[0]
                            elif (
                                uid in self.ds["dhcp"]
                                and self.ds["dhcp"][uid]["enabled"]
                                and self.ds["dhcp"][uid]["comment"].split("#", 1)[0]
                                != ""
                            ):
                                # Override name if DHCP comment exists
                                self.ds["host"][uid]["host-name"] = self.ds["dhcp"][
                                    uid
                                ]["comment"].split("#", 1)[0]
                            else:
                                self.ds["host"][uid]["host-name"] = dns_vals[
                                    "name"
                                ].split(".")[0]
                            break

                if self.ds["host"][uid]["host-name"] == "unknown":
                    # Resolve hostname from DHCP comment
                    if (
                        uid in self.ds["dhcp"]
                        and self.ds["dhcp"][uid]["enabled"]
                        and self.ds["dhcp"][uid]["comment"].split("#", 1)[0] != ""
                    ):
                        self.ds["host"][uid]["host-name"] = self.ds["dhcp"][uid][
                            "comment"
                        ].split("#", 1)[0]
                    # Resolve hostname from DHCP hostname
                    elif (
                        uid in self.ds["dhcp"]
                        and self.ds["dhcp"][uid]["enabled"]
                        and self.ds["dhcp"][uid]["host-name"] != "unknown"
                    ):
                        self.ds["host"][uid]["host-name"] = self.ds["dhcp"][uid][
                            "host-name"
                        ]
                    # Fallback to mac address for hostname
                    else:
                        self.ds["host"][uid]["host-name"] = uid

            # Resolve manufacturer
            if vals["manufacturer"] == "detect" and vals["mac-address"] != "unknown":
                self.ds["host"][uid]["manufacturer"] = (
                    await self.async_mac_lookup.lookup(vals["mac-address"])
                )

            if vals["manufacturer"] == "detect":
                self.ds["host"][uid]["manufacturer"] = ""

            # Count hosts
            if self.ds["host"][uid]["available"]:
                if vals["source"] in ["capsman", "wireless"]:
                    self.ds["resource"]["clients_wireless"] += 1
                else:
                    self.ds["resource"]["clients_wired"] += 1

    # ---------------------------
    #   process_accounting
    # ---------------------------
    def process_accounting(self) -> None:
        """Get Accounting data from Mikrotik"""
        # Check if accounting and account-local-traffic is enabled
        (
            accounting_enabled,
            local_traffic_enabled,
        ) = self.api.is_accounting_and_local_traffic_enabled()

        self._sync_client_traffic_hosts(
            {"available": False, "local_accounting": False}
        )

        _LOGGER.debug(
            f"Working with {len(self.ds['client_traffic'])} accounting devices"
        )

        # Per-IP totals; addresses shared by several hosts (such as
        # "unknown") cannot be attributed and are left out.
        uid_by_ip = {}
        for uid, vals in self.ds["client_traffic"].items():
            addr = vals.get("address")
            if not is_valid_ip(addr):
                continue
            uid_by_ip[addr] = None if addr in uid_by_ip else uid
        tmp_accounting_values = {
            addr: {"wan-tx": 0, "wan-rx": 0, "lan-tx": 0, "lan-rx": 0}
            for addr, uid in uid_by_ip.items()
            if uid
        }

        time_diff = self.api.take_client_traffic_snapshot(True)
        if time_diff:
            accounting_data = parse_api(
                data={},
                source=self.api.query("/ip/accounting/snapshot"),
                key=".id",
                vals=[
                    {"name": ".id"},
                    {"name": "src-address"},
                    {"name": "dst-address"},
                    {"name": "bytes", "default": 0},
                ],
            )

            threshold = self.api.accounting_threshold
            entry_count = len(accounting_data)

            if not threshold:
                pass
            elif entry_count == threshold:
                _LOGGER.warning(
                    f"Accounting entries count reached the threshold of {threshold}!"
                    " Some entries were not saved by Mikrotik so accounting calculation won't be correct."
                    " Consider shortening update interval or"
                    " increasing the accounting threshold value in Mikrotik."
                )
            elif entry_count > threshold * 0.9:
                _LOGGER.info(
                    f"Accounting entries count ({entry_count} reached 90% of the threshold,"
                    f" currently set to {threshold}! Consider shortening update interval or"
                    " increasing the accounting threshold value in Mikrotik."
                )

            networks = [
                vals["IPv4Network"]
                for vals in self.ds["dhcp-network"].values()
                if vals.get("IPv4Network")
            ]
            for item in accounting_data.values():
                source_ip = str(item.get("src-address")).strip()
                destination_ip = str(item.get("dst-address")).strip()
                try:
                    bits_count = int(str(item.get("bytes")).strip())
                except ValueError:
                    continue

                source_local = _address_in_networks(source_ip, networks)
                destination_local = _address_in_networks(destination_ip, networks)
                if source_local and destination_local:
                    # LAN TX/RX
                    if source_ip in tmp_accounting_values:
                        tmp_accounting_values[source_ip]["lan-tx"] += bits_count
                    if destination_ip in tmp_accounting_values:
                        tmp_accounting_values[destination_ip]["lan-rx"] += bits_count
                elif source_local:
                    # WAN TX
                    if source_ip in tmp_accounting_values:
                        tmp_accounting_values[source_ip]["wan-tx"] += bits_count
                elif destination_local and destination_ip in tmp_accounting_values:
                    # WAN RX
                    tmp_accounting_values[destination_ip]["wan-rx"] += bits_count

        # Calculate real throughput and transform it to appropriate unit
        # Also handle availability of accounting and local_accounting from Mikrotik
        for addr, vals in tmp_accounting_values.items():
            uid = uid_by_ip[addr]

            self.ds["client_traffic"][uid]["available"] = accounting_enabled
            self.ds["client_traffic"][uid]["local_accounting"] = local_traffic_enabled

            if not accounting_enabled or not time_diff:
                # Skip calculation for WAN and LAN if accounting is disabled
                continue

            self.ds["client_traffic"][uid]["wan-tx"] = (
                _round_rate(vals["wan-tx"] / time_diff) if vals["wan-tx"] else 0.0
            )
            self.ds["client_traffic"][uid]["wan-rx"] = (
                _round_rate(vals["wan-rx"] / time_diff) if vals["wan-rx"] else 0.0
            )

            if not local_traffic_enabled:
                # Skip calculation for LAN if LAN accounting is disabled
                continue

            self.ds["client_traffic"][uid]["lan-tx"] = (
                _round_rate(vals["lan-tx"] / time_diff) if vals["lan-tx"] else 0.0
            )
            self.ds["client_traffic"][uid]["lan-rx"] = (
                _round_rate(vals["lan-rx"] / time_diff) if vals["lan-rx"] else 0.0
            )

    # ---------------------------
    #   _sync_client_traffic_hosts
    # ---------------------------
    def _sync_client_traffic_hosts(self, defaults) -> None:
        """Add new hosts to client_traffic and refresh their address and name.

        Traffic is matched by IP, so a stale address would attribute it to
        whichever host got that address from DHCP since.
        """
        for uid, vals in list(self.ds["host"].items()):
            if uid not in self.ds["client_traffic"]:
                self.ds["client_traffic"][uid] = dict(defaults)
            self.ds["client_traffic"][uid].update(
                {
                    "address": vals.get("address", "unknown"),
                    "mac-address": vals.get("mac-address", uid),
                    "host-name": vals.get("host-name", "unknown"),
                }
            )

    # ---------------------------
    #   _get_iface_from_entry
    # ---------------------------
    def _get_iface_from_entry(self, entry):
        """Get interface default-name using name from interface dict"""
        uid = None
        for ifacename in self.ds["interface"]:
            if self.ds["interface"][ifacename]["name"] == entry["interface"]:
                uid = ifacename
                break

        return uid

    # ---------------------------
    #   process_kid_control
    # ---------------------------
    def process_kid_control_devices(self) -> None:
        """Get Kid Control Device data from Mikrotik"""

        self._sync_client_traffic_hosts(
            {
                # Per-device baseline: counters and the time they were read.
                # Unknown until a device has been seen twice.
                "previous-bytes-up": None,
                "previous-bytes-down": None,
                "previous-time": None,
                "tx": None,
                "rx": None,
                "available": False,
                "local_accounting": False,
            }
        )

        _LOGGER.debug(
            f"Working with {len(self.ds['client_traffic'])} kid control devices"
        )

        kid_control_devices_data = parse_api(
            data={},
            source=self.api.query("/ip/kid-control/device"),
            key="mac-address",
            vals=[
                {"name": "mac-address"},
                {"name": "bytes-down", "default": 0},
                {"name": "bytes-up", "default": 0},
                {
                    "name": "enabled",
                    "source": "disabled",
                    "type": "bool",
                    "reverse": True,
                },
            ],
        )

        # Keeps the shared snapshot clock moving; each device is timed on its own
        self.api.take_client_traffic_snapshot(False)
        now = monotonic()

        if not kid_control_devices_data:
            if "kid-control-devices" not in self.notified_flags:
                _LOGGER.error(
                    "No kid control devices found on your Mikrotik device, make sure kid-control feature is configured"
                )
                self.notified_flags.append("kid-control-devices")
            return
        elif "kid-control-devices" in self.notified_flags:
            self.notified_flags.remove("kid-control-devices")

        for uid, vals in kid_control_devices_data.items():
            if uid not in self.ds["client_traffic"]:
                _LOGGER.debug(f"Skipping unknown device {uid}")
                continue

            self.ds["client_traffic"][uid]["available"] = vals["enabled"]

            device = self.ds["client_traffic"][uid]
            elapsed = None
            if device.get("previous-time") is not None:
                elapsed = now - device["previous-time"]
                if elapsed < 1:
                    # Read twice in a moment (forced refresh): too short to rate
                    continue

            # A rate needs two samples of this device. Bytes since a missed
            # sample are divided by the time since the device's last sample,
            # and a new device starts from unknown instead of its lifetime
            # counter.
            for rate, counter, previous in (
                ("tx", "bytes-up", "previous-bytes-up"),
                ("rx", "bytes-down", "previous-bytes-down"),
            ):
                delta = _counter_delta(vals[counter], device.get(previous))
                if delta is not None and elapsed and elapsed > 0:
                    device[rate] = _round_rate(delta / elapsed)
                else:
                    device[rate] = None
                device[previous] = vals[counter]
            device["previous-time"] = now
