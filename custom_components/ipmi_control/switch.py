"""Switch platform for IPMI Controller."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HOST_NAME,
    CONF_POWER_CONTROL,
    DEFAULT_POWER_CONTROL,
    DOMAIN,
    POWER_CONTROL_BOTH,
    POWER_CONTROL_NONE,
    POWER_CONTROL_OFF,
    POWER_CONTROL_ON,
)
from .coordinator import IpmiDataUpdateCoordinator
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI power switch from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]
    client: IpmiClient = data["client"]

    power_control = entry.options.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)
    if power_control != POWER_CONTROL_NONE:
        async_add_entities([IpmiPowerSwitch(coordinator, entry, client)])


class IpmiPowerSwitch(CoordinatorEntity[IpmiDataUpdateCoordinator], SwitchEntity):
    """Switch to control IPMI host power."""

    _attr_assumed_state = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_icon = "mdi:server"

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_power"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def is_on(self) -> bool | None:
        """Return the power state."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("power")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the server."""
        power_control = self._entry.options.get(
            CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL
        )
        if power_control not in (POWER_CONTROL_BOTH, POWER_CONTROL_ON):
            raise HomeAssistantError("Power ON is not permitted by configuration")

        try:
            await self._client.power_on()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the server (soft/ACPI shutdown)."""
        power_control = self._entry.options.get(
            CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL
        )
        if power_control not in (POWER_CONTROL_BOTH, POWER_CONTROL_OFF):
            raise HomeAssistantError("Power OFF is not permitted by configuration")

        try:
            await self._client.power_off()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err
