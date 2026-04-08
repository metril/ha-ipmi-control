"""Sensor platform for IPMI Controller — fan speed with threshold attributes."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FANS, CONF_HOST_NAME, DOMAIN
from .coordinator import IpmiDataUpdateCoordinator

THRESHOLD_ATTR_MAP = {
    "lnr": "lower_non_recoverable",
    "lc": "lower_critical",
    "lnc": "lower_non_critical",
    "unc": "upper_non_critical",
    "uc": "upper_critical",
    "unr": "upper_non_recoverable",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI fan speed sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]

    fans = entry.options.get(CONF_FANS, [])
    entities = [
        IpmiFanSpeedSensor(coordinator, entry, fan["name"])
        for fan in fans
    ]
    if entities:
        async_add_entities(entities)


class IpmiFanSpeedSensor(
    CoordinatorEntity[IpmiDataUpdateCoordinator], SensorEntity
):
    """Sensor showing fan RPM with threshold attributes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fan"
    _attr_native_unit_of_measurement = "RPM"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        fan_name: str,
    ) -> None:
        """Initialize the fan speed sensor."""
        super().__init__(coordinator)
        self._fan_name = fan_name
        host_name = entry.data[CONF_HOST_NAME]
        safe_fan = fan_name.lower().replace(" ", "_")
        self._attr_unique_id = f"ipmi_{host_name}_{safe_fan}_speed"
        self._attr_name = f"{fan_name} Speed"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def native_value(self) -> int | None:
        """Return current fan RPM."""
        if self.coordinator.data is None:
            return None
        readings = self.coordinator.data.get("fan_readings", {})
        return readings.get(self._fan_name)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Return threshold values as human-readable attributes."""
        if self.coordinator.data is None:
            return None
        thresholds = self.coordinator.data.get("fan_thresholds", {}).get(
            self._fan_name
        )
        if not thresholds:
            return None
        return {
            THRESHOLD_ATTR_MAP[key]: value
            for key, value in thresholds.items()
            if key in THRESHOLD_ATTR_MAP
        }
