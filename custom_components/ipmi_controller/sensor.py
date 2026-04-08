"""Sensor platform for IPMI Controller — general SDR sensor support."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST_NAME, CONF_SENSORS, DOMAIN
from .coordinator import IpmiDataUpdateCoordinator

THRESHOLD_ATTR_MAP = {
    "lnr": "lower_non_recoverable",
    "lc": "lower_critical",
    "lnc": "lower_non_critical",
    "unc": "upper_non_critical",
    "uc": "upper_critical",
    "unr": "upper_non_recoverable",
}

# Map SDR unit strings to HA sensor properties
SDR_UNIT_MAP: dict[str, dict] = {
    "degrees C": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit": "\u00b0C",
        "icon": "mdi:thermometer",
    },
    "degrees F": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit": "\u00b0F",
        "icon": "mdi:thermometer",
    },
    "Volts": {
        "device_class": SensorDeviceClass.VOLTAGE,
        "native_unit": "V",
        "icon": "mdi:flash-triangle",
    },
    "RPM": {
        "device_class": None,
        "native_unit": "RPM",
        "icon": "mdi:fan",
    },
    "Watts": {
        "device_class": SensorDeviceClass.POWER,
        "native_unit": "W",
        "icon": "mdi:flash",
    },
    "Amps": {
        "device_class": SensorDeviceClass.CURRENT,
        "native_unit": "A",
        "icon": "mdi:flash",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI SDR sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]

    sensors = entry.options.get(CONF_SENSORS, [])
    entities = [
        IpmiSdrSensor(coordinator, entry, sensor["name"], sensor.get("unit", ""))
        for sensor in sensors
    ]
    if entities:
        async_add_entities(entities)


class IpmiSdrSensor(
    CoordinatorEntity[IpmiDataUpdateCoordinator], SensorEntity
):
    """Sensor entity for any IPMI SDR sensor reading."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        sensor_name: str,
        unit: str,
    ) -> None:
        """Initialize the SDR sensor."""
        super().__init__(coordinator)
        self._sensor_name = sensor_name
        host_name = entry.data[CONF_HOST_NAME]
        safe_name = sensor_name.lower().replace(" ", "_")
        self._attr_unique_id = f"ipmi_{host_name}_{safe_name}"
        self._attr_name = sensor_name

        # Map SDR unit to HA properties
        unit_config = SDR_UNIT_MAP.get(unit, {})
        self._attr_device_class = unit_config.get("device_class")
        self._attr_native_unit_of_measurement = unit_config.get("native_unit", unit or None)
        self._attr_icon = unit_config.get("icon", "mdi:chip")

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def native_value(self) -> float | None:
        """Return current sensor reading."""
        if self.coordinator.data is None:
            return None
        reading = self.coordinator.data.get("sensor_readings", {}).get(
            self._sensor_name
        )
        if reading is None:
            return None
        return reading.get("value")

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Return threshold values as human-readable attributes."""
        if self.coordinator.data is None:
            return None
        thresholds = self.coordinator.data.get("sensor_thresholds", {}).get(
            self._sensor_name
        )
        if not thresholds:
            return None
        return {
            THRESHOLD_ATTR_MAP[key]: value
            for key, value in thresholds.items()
            if key in THRESHOLD_ATTR_MAP
        }
