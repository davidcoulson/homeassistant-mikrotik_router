"""Review 2: data correctness fixes (SFP temperature, client traffic, ...)."""

from unittest.mock import MagicMock

from custom_components.mikrotik_router.coordinator import MikrotikCoordinator
from custom_components.mikrotik_router.helper import is_sfp_port
from custom_components.mikrotik_router.sensor import MikrotikDiagnosticSensor
from custom_components.mikrotik_router.sensor_types import SENSOR_TYPES


def _coordinator(interfaces):
    coordinator = object.__new__(MikrotikCoordinator)
    coordinator.ds = {"interface": interfaces}
    coordinator.api = MagicMock()
    coordinator._monitor_batch = True
    return coordinator


# M7: copper ports and empty cages report no SFP temperature
def test_ethernet_config_leaves_copper_ports_without_sfp_marker() -> None:
    coordinator = _coordinator(
        {
            "ether1": {"name": "ether1", "default-name": "ether1", "type": "ether"},
            "sfp1": {"name": "sfp1", "default-name": "sfp1", "type": "ether"},
        }
    )
    coordinator.api.query.return_value = [
        {"default-name": "ether1", "name": "ether1"},
        {"default-name": "sfp1", "name": "sfp1", "sfp-shutdown-temperature": 95},
    ]
    coordinator._get_ethernet_config()
    assert coordinator.ds["interface"]["ether1"]["sfp-shutdown-temperature"] is None
    assert coordinator.ds["interface"]["sfp1"]["sfp-shutdown-temperature"] == 95
    assert not is_sfp_port(coordinator.ds["interface"]["ether1"])
    assert is_sfp_port(coordinator.ds["interface"]["sfp1"])


def test_monitor_without_module_reports_none_temperature() -> None:
    from custom_components.mikrotik_router.coordinator import ETHERNET_MONITOR_SFP_VALS

    coordinator = _coordinator(
        {"sfp1": {"name": "sfp1", "default-name": "sfp1", "type": "ether"}}
    )
    # An empty cage: no sfp-* fields at all in the monitor reply
    coordinator.api.query.return_value = [{"name": "sfp1", "status": "no-link"}]
    coordinator._monitor_ethernet(["*1"], ETHERNET_MONITOR_SFP_VALS)
    assert coordinator.ds["interface"]["sfp1"]["sfp-temperature"] is None

    sensor = object.__new__(MikrotikDiagnosticSensor)
    sensor.entity_description = next(d for d in SENSOR_TYPES if d.key == "sfp_temperature")
    sensor._data = coordinator.ds["interface"]["sfp1"]
    assert sensor.native_value is None

    coordinator.api.query.return_value = [
        {"name": "sfp1", "status": "link-ok", "sfp-temperature": 41}
    ]
    coordinator._monitor_ethernet(["*1"], ETHERNET_MONITOR_SFP_VALS)
    sensor._data = coordinator.ds["interface"]["sfp1"]
    assert sensor.native_value == 41
