"""Button platform for IPMI Controller."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_FANS, CONF_HOST_NAME, DOMAIN
from .ipmi import IpmiAuthError, IpmiClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI button entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    client: IpmiClient = data["client"]

    fans = entry.options.get(CONF_FANS, [])
    if fans:
        async_add_entities([IpmiSetThresholdsButton(entry, client)])


class IpmiSetThresholdsButton(ButtonEntity):
    """Button to apply fan sensor thresholds."""

    _attr_has_entity_name = True
    _attr_name = "Set Fan Thresholds"
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, entry: ConfigEntry, client: IpmiClient) -> None:
        """Initialize the thresholds button."""
        self._client = client
        self._entry = entry
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_set_fan_thresholds"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    async def async_press(self) -> None:
        """Apply all configured fan thresholds."""
        fans = self._entry.options.get(CONF_FANS, [])
        if not fans:
            _LOGGER.info("No fans configured, nothing to do")
            return

        try:
            result = await self.hass.async_add_executor_job(
                self._client.set_fan_thresholds, fans
            )
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err

        if not result:
            raise HomeAssistantError("Some fan threshold settings failed")

        _LOGGER.info("Fan thresholds applied successfully")
