"""Binary sensor platform for IPMI Controller."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST_NAME, DOMAIN
from .coordinator import IpmiDataUpdateCoordinator
from .data import IpmiConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpmiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI binary sensor from a config entry."""
    async_add_entities(
        [IpmiPowerBinarySensor(entry.runtime_data.coordinator, entry)]
    )


class IpmiPowerBinarySensor(
    CoordinatorEntity[IpmiDataUpdateCoordinator], BinarySensorEntity
):
    """Binary sensor showing IPMI host power state."""

    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_has_entity_name = True
    _attr_translation_key = "power_state"

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: IpmiConfigEntry,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_power_state"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the server is powered on."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("power")
