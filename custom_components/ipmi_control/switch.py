"""Switch platform for IPMI Control."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HARD_OFF_DISARM_TIMEOUT,
    CONF_POWER_CONTROL,
    CONF_POWER_STATE_HOLD,
    CONF_PRIVILEGE_LEVEL,
    DEFAULT_HARD_OFF_DISARM_TIMEOUT,
    DEFAULT_POWER_CONTROL,
    DEFAULT_POWER_STATE_HOLD,
    POWER_HARD_OFF,
    POWER_ON,
    POWER_SOFT_OFF,
    device_info_for,
    signal_disarmed,
)
from .coordinator import IpmiDataUpdateCoordinator
from .data import IpmiConfigEntry
from .ipmi import IpmiAuthError, IpmiClient

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpmiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI power switch from a config entry."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client

    policy: list[str] = entry.options.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)

    entities: list[SwitchEntity] = []

    if POWER_ON in policy or POWER_SOFT_OFF in policy:
        entities.append(IpmiPowerSwitch(coordinator, entry, client))

    if POWER_HARD_OFF in policy:
        entities.append(IpmiArmHardOffSwitch(hass, entry))

    # BMC cold reset is an Administrator operation, like fan control and
    # threshold writes. Operator entries never get the entity pair.
    if entry.data.get(CONF_PRIVILEGE_LEVEL) == "ADMINISTRATOR":
        entities.append(IpmiArmBmcResetSwitch(hass, entry))

    if entities:
        async_add_entities(entities)


class IpmiPowerSwitch(CoordinatorEntity[IpmiDataUpdateCoordinator], SwitchEntity):
    """Switch to control IPMI host power."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_has_entity_name = True
    _attr_translation_key = "power"

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: IpmiConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        self._optimistic_state: bool | None = None
        self._optimistic_expiry: float = 0
        self._attr_unique_id = f"{entry.entry_id}_power"
        self._attr_device_info = device_info_for(entry)

    @property
    def is_on(self) -> bool | None:
        """Return the power state."""
        if self.coordinator.data is None:
            return None
        actual = self.coordinator.data.get("power")
        if (
            self._optimistic_state is not None
            and actual != self._optimistic_state
            and time.monotonic() < self._optimistic_expiry
        ):
            return self._optimistic_state
        return actual

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic override once confirmed or expired."""
        if self._optimistic_state is not None and (
            self.coordinator.data is None
            or self.coordinator.data.get("power") == self._optimistic_state
            or time.monotonic() >= self._optimistic_expiry
        ):
            self._optimistic_state = None
            self._optimistic_expiry = 0
        super()._handle_coordinator_update()

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
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

        self._set_optimistic(True)
        await self.coordinator.async_request_refresh()

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
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

        self._set_optimistic(False)
        await self.coordinator.async_request_refresh()

    def _set_optimistic(self, state: bool) -> None:
        """Set optimistic state override with configured hold duration."""
        hold = self._entry.options.get(
            CONF_POWER_STATE_HOLD, DEFAULT_POWER_STATE_HOLD
        )
        if hold > 0:
            self._optimistic_state = state
            self._optimistic_expiry = time.monotonic() + hold
        else:
            self._optimistic_state = None
            self._optimistic_expiry = 0
        self.async_write_ha_state()


class IpmiArmSwitch(SwitchEntity):
    """Toggle that arms a destructive action for a short window.

    Subclasses supply the runtime_data flag they own and their own identity. The
    flag is read live from runtime_data rather than mirrored on the entity so
    the button, the arm switch, and the domain services always agree.
    """

    _attr_has_entity_name = True

    _arm_key: str
    _unique_id_suffix: str

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IpmiConfigEntry,
    ) -> None:
        """Initialize the arm switch."""
        self._hass = hass
        self._entry = entry
        self._disarm_cancel: CALLBACK_TYPE | None = None
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_id_suffix}"
        self._attr_device_info = device_info_for(entry)

    @property
    def is_on(self) -> bool:
        """Return whether the action is armed."""
        return getattr(self._entry.runtime_data, self._arm_key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm the action."""
        setattr(self._entry.runtime_data, self._arm_key, True)

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
        """Disarm the action."""
        self._disarm(write_state=True)

    def _disarm(self, write_state: bool = False) -> None:
        """Disarm and cancel the timer."""
        setattr(self._entry.runtime_data, self._arm_key, False)
        if self._disarm_cancel is not None:
            self._disarm_cancel()
            self._disarm_cancel = None
        if write_state:
            self.async_write_ha_state()

    def _auto_disarm(self, _now: Any) -> None:
        """Auto-disarm callback after timeout."""
        self._disarm_cancel = None
        setattr(self._entry.runtime_data, self._arm_key, False)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to disarm notifications from the buttons and services."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_disarmed(self._entry.entry_id),
                self._handle_disarmed,
            )
        )

    @callback
    def _handle_disarmed(self) -> None:
        """The flag was consumed elsewhere: cancel the timer and refresh state."""
        if self._disarm_cancel is not None:
            self._disarm_cancel()
            self._disarm_cancel = None
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up on removal."""
        if self._disarm_cancel is not None:
            self._disarm_cancel()
            self._disarm_cancel = None


class IpmiArmHardOffSwitch(IpmiArmSwitch):
    """Toggle that arms the force power off capability."""

    _attr_translation_key = "arm_hard_off"
    _arm_key = "hard_off_armed"
    _unique_id_suffix = "arm_hard_off"


class IpmiArmBmcResetSwitch(IpmiArmSwitch):
    """Toggle that arms the BMC cold reset capability."""

    _attr_translation_key = "arm_bmc_reset"
    _arm_key = "bmc_reset_armed"
    _unique_id_suffix = "arm_bmc_reset"
