"""Sensor platform for IPMI Controller — read-only fan threshold display."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FANS, CONF_HOST_NAME, DOMAIN
from .coordinator import IpmiDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI threshold sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]

    fans = entry.options.get(CONF_FANS, [])
    entities = [
        IpmiFanThresholdSensor(coordinator, entry, fan["name"])
        for fan in fans
    ]
    if entities:
        async_add_entities(entities)


class IpmiFanThresholdSensor(
    CoordinatorEntity[IpmiDataUpdateCoordinator], SensorEntity
):
    """Sensor showing actual BMC threshold values for a fan."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-lines"

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        fan_name: str,
    ) -> None:
        """Initialize the threshold sensor."""
        super().__init__(coordinator)
        self._fan_name = fan_name
        host_name = entry.data[CONF_HOST_NAME]
        safe_fan = fan_name.lower().replace(" ", "_")
        self._attr_unique_id = f"ipmi_{host_name}_{safe_fan}_thresholds"
        self._attr_name = f"{fan_name} Thresholds"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def native_value(self) -> str | None:
        """Return a summary of current thresholds."""
        thresholds = self._get_thresholds()
        if thresholds is None:
            return None
        lower = f"{thresholds['lnr']}/{thresholds['lc']}/{thresholds['lnc']}"
        upper = f"{thresholds['unc']}/{thresholds['uc']}/{thresholds['unr']}"
        return f"{lower} | {upper}"

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Return individual threshold values as attributes."""
        return self._get_thresholds()

    def _get_thresholds(self) -> dict[str, int] | None:
        """Get thresholds for this fan from coordinator data."""
        if self.coordinator.data is None:
            return None
        fan_thresholds = self.coordinator.data.get("fan_thresholds", {})
        return fan_thresholds.get(self._fan_name)
