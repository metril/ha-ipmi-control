"""Button platform for IPMI Controller."""

from __future__ import annotations

import logging
import time

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BMC_RESET_GRACE,
    CONF_HOST_NAME,
    CONF_POWER_CONTROL,
    CONF_PRIVILEGE_LEVEL,
    CONF_SENSORS,
    DEFAULT_BMC_RESET_GRACE,
    DEFAULT_POWER_CONTROL,
    DOMAIN,
    POWER_HARD_OFF,
)
from .coordinator import IpmiDataUpdateCoordinator
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)


async def async_execute_bmc_cold_reset(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: IpmiClient,
) -> None:
    """Cold reset the BMC, if armed, and open the post-reset grace window.

    Shared by the button and the bmc_cold_reset service so both enforce the arm
    gate identically and both start the grace period the coordinator relies on.
    Callers that legitimately bypass the arm gate (the service's confirm: true
    path) set the flag themselves before calling.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if not entry_data.get("bmc_reset_armed", False):
        raise HomeAssistantError("BMC cold reset is not armed")

    try:
        await client.bmc_cold_reset()
    except IpmiAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(str(err)) from err
    except IpmiConnectionError as err:
        raise HomeAssistantError(str(err)) from err
    except Exception as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        entry_data["bmc_reset_armed"] = False

    grace = entry.options.get(CONF_BMC_RESET_GRACE, DEFAULT_BMC_RESET_GRACE)
    entry_data["bmc_reset_grace_until"] = time.monotonic() + grace
    _LOGGER.info(
        "BMC cold reset issued for %s; tolerating connection failures for %ss",
        entry.data[CONF_HOST_NAME],
        grace,
    )


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

    if privilege == "ADMINISTRATOR":
        entities.append(IpmiBmcColdResetButton(hass, entry, client))

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
            await self._coordinator.async_refresh_thresholds()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

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
    _attr_name = "Power Off (Force)"
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
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

        entry_data["hard_off_armed"] = False
        _LOGGER.info("Hard power off executed")


class IpmiBmcColdResetButton(ButtonEntity):
    """Button to cold reset the BMC itself (requires arming first)."""

    _attr_has_entity_name = True
    _attr_name = "BMC Cold Reset"
    _attr_icon = "mdi:restart-alert"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the BMC cold reset button."""
        self._hass = hass
        self._client = client
        self._entry = entry
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_bmc_cold_reset"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    async def async_press(self) -> None:
        """Cold reset the BMC if armed."""
        await async_execute_bmc_cold_reset(self._hass, self._entry, self._client)
