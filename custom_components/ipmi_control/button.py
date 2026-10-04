"""Button platform for IPMI Controller."""

from __future__ import annotations

import logging
import time

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
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
    device_info_for,
    signal_disarmed,
)
from .coordinator import IpmiDataUpdateCoordinator
from .data import IpmiConfigEntry
from .ipmi import IpmiAuthError, IpmiClient

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_execute_bmc_cold_reset(
    hass: HomeAssistant,
    entry: IpmiConfigEntry,
    client: IpmiClient,
    bypass_arm: bool = False,
) -> None:
    """Cold reset the BMC, if armed, and open the post-reset grace window.

    Shared by the button and the bmc_cold_reset service so both enforce the arm
    gate identically. The arm flag is consumed (and the arm switch notified)
    before the IPMI call so concurrent presses cannot both pass the gate.
    """
    runtime = entry.runtime_data
    if not bypass_arm and not runtime.bmc_reset_armed:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_armed"
        )
    runtime.bmc_reset_armed = False
    async_dispatcher_send(hass, signal_disarmed(entry.entry_id))

    try:
        await client.bmc_cold_reset()
    except IpmiAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(str(err)) from err
    except Exception as err:
        raise HomeAssistantError(str(err)) from err

    grace = entry.options.get(CONF_BMC_RESET_GRACE, DEFAULT_BMC_RESET_GRACE)
    runtime.bmc_reset_grace_until = time.monotonic() + grace
    _LOGGER.info(
        "BMC cold reset issued for %s; tolerating connection failures for %ss",
        entry.data[CONF_HOST_NAME],
        grace,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpmiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI button entities from a config entry."""
    client = entry.runtime_data.client
    coordinator = entry.runtime_data.coordinator

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
    _attr_translation_key = "set_sensor_thresholds"

    def __init__(
        self,
        entry: IpmiConfigEntry,
        client: IpmiClient,
        coordinator: IpmiDataUpdateCoordinator,
    ) -> None:
        """Initialize the thresholds button."""
        self._client = client
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_set_sensor_thresholds"
        self._attr_device_info = device_info_for(entry)

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
        except ConfigEntryAuthFailed as err:
            self._coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

        _LOGGER.info("Sensor thresholds applied successfully")


class IpmiRefreshThresholdsButton(ButtonEntity):
    """Diagnostic button to manually refresh sensor thresholds from BMC."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_sensor_thresholds"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: IpmiConfigEntry,
        coordinator: IpmiDataUpdateCoordinator,
    ) -> None:
        """Initialize the refresh button."""
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh_sensor_thresholds"
        self._attr_device_info = device_info_for(entry)

    async def async_press(self) -> None:
        """Refresh sensor thresholds from BMC."""
        try:
            await self._coordinator.async_refresh_thresholds()
        except ConfigEntryAuthFailed as err:
            self._coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info("Sensor thresholds refreshed from BMC")


class IpmiForceHardOffButton(ButtonEntity):
    """Button to force hard power off (requires arming first)."""

    _attr_has_entity_name = True
    _attr_translation_key = "force_hard_off"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IpmiConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the force hard off button."""
        self._hass = hass
        self._client = client
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_force_hard_off"
        self._attr_device_info = device_info_for(entry)

    async def async_press(self) -> None:
        """Execute hard power off if armed."""
        runtime = self._entry.runtime_data
        if not runtime.hard_off_armed:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="not_armed"
            )
        # Consume the arm flag before the (slow) IPMI call so a concurrent
        # press cannot also pass the gate.
        runtime.hard_off_armed = False
        async_dispatcher_send(self._hass, signal_disarmed(self._entry.entry_id))

        try:
            await self._client.hard_power_off()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

        _LOGGER.info("Hard power off executed")


class IpmiBmcColdResetButton(ButtonEntity):
    """Button to cold reset the BMC itself (requires arming first)."""

    _attr_has_entity_name = True
    _attr_translation_key = "bmc_cold_reset"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IpmiConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the BMC cold reset button."""
        self._hass = hass
        self._client = client
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_bmc_cold_reset"
        self._attr_device_info = device_info_for(entry)

    async def async_press(self) -> None:
        """Cold reset the BMC if armed."""
        await async_execute_bmc_cold_reset(self._hass, self._entry, self._client)
