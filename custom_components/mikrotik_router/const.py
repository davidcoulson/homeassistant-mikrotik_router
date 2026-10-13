"""Constants used by the Mikrotik Router component and platforms."""

from homeassistant.const import Platform

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.UPDATE,
]

DOMAIN = "mikrotik_router"
DEFAULT_NAME = "Mikrotik Router"
ATTRIBUTION = "Data provided by Mikrotik"

DEFAULT_ENCODING = "ISO-8859-1"
DEFAULT_LOGIN_METHOD = "plain"

DEFAULT_HOST = "10.0.0.1"
DEFAULT_USERNAME = "admin"
DEFAULT_PORT = 0
DEFAULT_DEVICE_NAME = "Mikrotik"
# New entries use SSL with certificate verification (M1)
DEFAULT_SSL = True
DEFAULT_VERIFY_SSL = True
# Entries created before verify_ssl existed never verified certificates
LEGACY_VERIFY_SSL = False
# Option of an earlier build, replaced by allowed_changes (migrated in __init__)
LEGACY_READ_ONLY = "read_only"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 30
CONF_DEVICE_TRACKER_SCAN_INTERVAL = "device_tracker_scan_interval"
DEFAULT_DEVICE_TRACKER_SCAN_INTERVAL = 10
CONF_TRACK_IFACE_CLIENTS = "track_iface_clients"
DEFAULT_TRACK_IFACE_CLIENTS = True
CONF_TRACK_HOSTS = "track_network_hosts"
DEFAULT_TRACK_HOSTS = False
CONF_TRACK_HOSTS_TIMEOUT = "track_network_hosts_timeout"
DEFAULT_TRACK_HOST_TIMEOUT = 180
MAX_TRACK_HOST_TIMEOUT = 86400
# Which kinds of change Home Assistant may make on the router. All are
# allowed by default, so existing entries behave as before; none is
# read-only.
CONF_ALLOWED_CHANGES = "allowed_changes"
CHANGE_PORTS = "ports"
CHANGE_FIREWALL = "firewall"
CHANGE_QUEUES = "queues"
CHANGE_KIDCONTROL = "kidcontrol"
CHANGE_PPP = "ppp"
CHANGE_SCRIPTS = "scripts"
CHANGE_FIRMWARE = "firmware"
CHANGE_ACCOUNTING = "accounting"
ALL_CHANGES = (
    CHANGE_PORTS,
    CHANGE_FIREWALL,
    CHANGE_QUEUES,
    CHANGE_KIDCONTROL,
    CHANGE_PPP,
    CHANGE_SCRIPTS,
    CHANGE_FIRMWARE,
    CHANGE_ACCOUNTING,
)
DEFAULT_ALLOWED_CHANGES = list(ALL_CHANGES)

# The RouterOS path each write goes to, and the change it belongs to. A
# restricted connection refuses writes to any path not listed here.
WRITE_PATH_CHANGES = {
    "/interface": CHANGE_PORTS,
    "/interface/ethernet": CHANGE_PORTS,
    "/ip/firewall/nat": CHANGE_FIREWALL,
    "/ip/firewall/mangle": CHANGE_FIREWALL,
    "/ip/firewall/filter": CHANGE_FIREWALL,
    "/queue/simple": CHANGE_QUEUES,
    "/ip/kid-control": CHANGE_KIDCONTROL,
    "/ppp/secret": CHANGE_PPP,
    "/system/script": CHANGE_SCRIPTS,
    "/system/package/update": CHANGE_FIRMWARE,
    "/system/routerboard": CHANGE_FIRMWARE,
    "/system/backup": CHANGE_FIRMWARE,
    "/system": CHANGE_FIRMWARE,
    "/ip/accounting": CHANGE_ACCOUNTING,
}

CONF_SENSOR_PORT_TRACKER = "sensor_port_tracker"
DEFAULT_SENSOR_PORT_TRACKER = False
CONF_SENSOR_PORT_TRAFFIC = "sensor_port_traffic"
DEFAULT_SENSOR_PORT_TRAFFIC = False
CONF_SENSOR_CLIENT_TRAFFIC = "sensor_client_traffic"
DEFAULT_SENSOR_CLIENT_TRAFFIC = False
CONF_SENSOR_CLIENT_CAPTIVE = "sensor_client_captive"
DEFAULT_SENSOR_CLIENT_CAPTIVE = False
CONF_SENSOR_SIMPLE_QUEUES = "sensor_simple_queues"
DEFAULT_SENSOR_SIMPLE_QUEUES = False
CONF_SENSOR_NAT = "sensor_nat"
DEFAULT_SENSOR_NAT = False
CONF_SENSOR_MANGLE = "sensor_mangle"
DEFAULT_SENSOR_MANGLE = False
CONF_SENSOR_FILTER = "sensor_filter"
DEFAULT_SENSOR_FILTER = False
CONF_SENSOR_PPP = "sensor_ppp"
DEFAULT_SENSOR_PPP = False
CONF_SENSOR_KIDCONTROL = "sensor_kidcontrol"
DEFAULT_SENSOR_KIDCONTROL = False
CONF_SENSOR_SCRIPTS = "sensor_scripts"
DEFAULT_SENSOR_SCRIPTS = False
CONF_SENSOR_ENVIRONMENT = "sensor_environment"
DEFAULT_SENSOR_ENVIRONMENT = False
CONF_SENSOR_NETWATCH_TRACKER = "sensor_netwatch_tracker"
DEFAULT_SENSOR_NETWATCH_TRACKER = False

TO_REDACT = {
    "ip-address",
    "client-ip-address",
    "address",
    "active-address",
    "mac-address",
    "active-mac-address",
    "orig-mac-address",
    "port-mac-address",
    "client-mac-address",
    "client-id",
    "active-client-id",
    "host-name",
    "eeprom",
    "sfp-vendor-serial",
    "gateway",
    "dns-server",
    "wins-server",
    "ntp-server",
    "caps-manager",
    "serial-number",
    "source",
    "from-addresses",
    "to-addresses",
    "src-address",
    "dst-address",
    "username",
    "password",
    "caller-id",
    "target",
    "ssid",
    "latitude",
    "longitude",
}
