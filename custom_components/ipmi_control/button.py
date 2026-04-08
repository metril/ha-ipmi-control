"""Button platform for IPMI Controller."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_HOST_NAME,
    CONF_POWER_CONTROL,
    CONF_PRIVILEGE_LEVEL,
    CONF_SENSORS,
    DEFAULT_POWER_CONTROL,
    DOMAIN,
    POWER_HARD_OFF,
)
from .coordinator import IpmiDataUpdateCoordinator
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI button entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    client: IpmiClient = data["client"]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]

    sensors = entry.options.get(CONF_SENSORS, [])
    privilege = entry.data.get(CONF_PRIVILEGE_LEVEL, "ADMINISTRATOR")
    policy: list[str] = entry.options.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)
    entities: list[ButtonEntity] = []

    if sensors:
        entities.append(IpmiRefreshThresholdsButton(entry, coordinator))

    sensors_with_thresholds = [s for s in sensors if s.get("thresholds")]
    if sensors_with_thresholds and privilege == "ADMINISTRATOR":
        entities.append(IpmiSetThresholdsButton(entry, client, coordinator))

    if POWER_HARD_OFF in policy:
        entities.append(IpmiForceHardOffButton(hass, entry, client))

    if entities:
        async_add_entities(entities)


class IpmiSetThresholdsButton(ButtonEntity):
    """Button to apply sensor threshold overrides."""

    _attr_has_entity_name = True
    _attr_name = "Set Sensor Thresholds"
    _attr_icon = "mdi:thermometer-lines"

    def __init__(
        self,
        entry: ConfigEntry,
        client: IpmiClient,
        coordinator: IpmiDataUpdateCoordinator,
    ) -> None:
        """Initialize the thresholds button."""
        self._client = client
        self._entry = entry
        self._coordinator = coordinator
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_set_sensor_thresholds"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    async def async_press(self) -> None:
        """Apply all configured sensor thresholds."""
        sensors = self._entry.options.get(CONF_SENSORS, [])
        sensors_with_thresholds = [s for s in sensors if s.get("thresholds")]
        if not sensors_with_thresholds:
            _LOGGER.info("No sensor thresholds configured, nothing to do")
            return

        try:
            await self._client.set_sensor_thresholds(sensors_with_thresholds)
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err

        await self._coordinator.async_refresh_thresholds()
        _LOGGER.info("Sensor thresholds applied successfully")


class IpmiRefreshThresholdsButton(ButtonEntity):
    """Diagnostic button to manually refresh sensor thresholds from BMC."""

    _attr_has_entity_name = True
    _attr_name = "Refresh Sensor Thresholds"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: IpmiDataUpdateCoordinator,
    ) -> None:
        """Initialize the refresh button."""
        self._entry = entry
        self._coordinator = coordinator
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_refresh_sensor_thresholds"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    async def async_press(self) -> None:
        """Refresh sensor thresholds from BMC."""
        try:
            await self._coordinator.async_refresh_thresholds()
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info("Sensor thresholds refreshed from BMC")


class IpmiForceHardOffButton(ButtonEntity):
    """Button to force hard power off (requires arming first)."""

    _attr_has_entity_name = True
    _attr_name = "Force Power Off"
    _attr_icon = "mdi:power-off"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the force hard off button."""
        self._hass = hass
        self._client = client
        self._entry = entry
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_force_hard_off"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    async def async_press(self) -> None:
        """Execute hard power off if armed."""
        entry_data = self._hass.data[DOMAIN][self._entry.entry_id]
        if not entry_data.get("hard_off_armed", False):
            raise HomeAssistantError("Force power off is not armed")

        try:
            await self._client.hard_power_off()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err

        entry_data["hard_off_armed"] = False
        _LOGGER.info("Hard power off executed")
