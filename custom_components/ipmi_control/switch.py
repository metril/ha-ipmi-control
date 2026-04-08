"""Switch platform for IPMI Control."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HARD_OFF_DISARM_TIMEOUT,
    CONF_HOST_NAME,
    CONF_POWER_CONTROL,
    CONF_POWER_STATE_HOLD,
    DEFAULT_HARD_OFF_DISARM_TIMEOUT,
    DEFAULT_POWER_CONTROL,
    DEFAULT_POWER_STATE_HOLD,
    DOMAIN,
    POWER_HARD_OFF,
    POWER_ON,
    POWER_SOFT_OFF,
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

    policy: list[str] = entry.options.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)

    entities: list[SwitchEntity] = []

    if POWER_ON in policy or POWER_SOFT_OFF in policy:
        entities.append(IpmiPowerSwitch(coordinator, entry, client))

    if POWER_HARD_OFF in policy:
        entities.append(IpmiArmHardOffSwitch(hass, entry))

    if entities:
        async_add_entities(entities)


class IpmiPowerSwitch(CoordinatorEntity[IpmiDataUpdateCoordinator], SwitchEntity):
    """Switch to control IPMI host power."""

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
        self._optimistic_state: bool | None = None
        self._optimistic_expiry: float = 0
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
        actual = self.coordinator.data.get("power")
        if self._optimistic_state is not None:
            if actual == self._optimistic_state:
                # BMC caught up, clear override
                self._optimistic_state = None
                self._optimistic_expiry = 0
            elif time.monotonic() < self._optimistic_expiry:
                return self._optimistic_state
            else:
                # Expired without confirmation, clear override
                self._optimistic_state = None
                self._optimistic_expiry = 0
        return actual

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the server."""
        policy: list[str] = self._entry.options.get(
            CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL
        )
        if POWER_ON not in policy:
            raise HomeAssistantError("Power ON is not permitted by configuration")

        try:
            await self._client.power_on()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err

        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the server (soft/ACPI shutdown)."""
        policy: list[str] = self._entry.options.get(
            CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL
        )
        if POWER_SOFT_OFF not in policy:
            raise HomeAssistantError("Power OFF is not permitted by configuration")

        try:
            await self._client.power_off()
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except IpmiConnectionError as err:
            raise HomeAssistantError(str(err)) from err

        self._set_optimistic(False)

    def _set_optimistic(self, state: bool) -> None:
        """Set optimistic state override with configured hold duration."""
        hold = self._entry.options.get(
            CONF_POWER_STATE_HOLD, DEFAULT_POWER_STATE_HOLD
        )
        if hold > 0:
            self._optimistic_state = state
            self._optimistic_expiry = time.monotonic() + hold
            self.async_write_ha_state()


class IpmiArmHardOffSwitch(SwitchEntity):
    """Toggle that arms the force power off capability."""

    _attr_has_entity_name = True
    _attr_name = "Arm Force Power Off"
    _attr_icon = "mdi:shield-alert"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the arm switch."""
        self._hass = hass
        self._entry = entry
        self._disarm_cancel: CALLBACK_TYPE | None = None
        host_name = entry.data[CONF_HOST_NAME]
        self._attr_unique_id = f"ipmi_{host_name}_arm_hard_off"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def is_on(self) -> bool:
        """Return whether hard power off is armed."""
        return self._hass.data[DOMAIN][self._entry.entry_id].get(
            "hard_off_armed", False
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm the force power off."""
        self._hass.data[DOMAIN][self._entry.entry_id]["hard_off_armed"] = True

        # Cancel any existing disarm timer
        if self._disarm_cancel is not None:
            self._disarm_cancel()

        timeout = self._entry.options.get(
            CONF_HARD_OFF_DISARM_TIMEOUT, DEFAULT_HARD_OFF_DISARM_TIMEOUT
        )
        self._disarm_cancel = async_call_later(
            self._hass, timeout, self._auto_disarm
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm the force power off."""
        self._disarm(write_state=True)

    def _disarm(self, write_state: bool = False) -> None:
        """Disarm and cancel the timer."""
        self._hass.data[DOMAIN][self._entry.entry_id]["hard_off_armed"] = False
        if self._disarm_cancel is not None:
            self._disarm_cancel()
            self._disarm_cancel = None
        if write_state:
            self.async_write_ha_state()

    def _auto_disarm(self, _now: Any) -> None:
        """Auto-disarm callback after timeout."""
        self._disarm_cancel = None
        self._hass.data[DOMAIN][self._entry.entry_id]["hard_off_armed"] = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up on removal."""
        if self._disarm_cancel is not None:
            self._disarm_cancel()
            self._disarm_cancel = None
