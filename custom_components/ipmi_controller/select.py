"""Select platform for IPMI Controller fan mode."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_FAN_MODE_DISPLAY_MAPPING,
    CONF_FAN_MODES,
    CONF_HOST_NAME,
    CONF_VIRTUAL_MODE_MAPPING,
    DOMAIN,
)
from .coordinator import IpmiDataUpdateCoordinator
from .ipmi import IpmiAuthError, IpmiClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI fan mode select from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]
    client: IpmiClient = data["client"]

    fan_modes = entry.options.get(CONF_FAN_MODES, [])
    if fan_modes:
        async_add_entities([IpmiFanModeSelect(coordinator, entry, client)])


class IpmiFanModeSelect(CoordinatorEntity[IpmiDataUpdateCoordinator], SelectEntity):
    """Select entity for IPMI fan mode control."""

    _attr_has_entity_name = True
    _attr_name = "Fan Mode"
    _attr_icon = "mdi:fan"

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the fan mode select."""
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        host_name = entry.data[CONF_HOST_NAME]

        self._display_mapping: dict[str, str] = entry.options.get(
            CONF_FAN_MODE_DISPLAY_MAPPING, {}
        )
        self._virtual_mode_mapping: dict[str, str] = entry.options.get(
            CONF_VIRTUAL_MODE_MAPPING, {}
        )
        fan_modes: list[str] = entry.options.get(CONF_FAN_MODES, [])

        # Build options list using display names
        self._attr_options = [
            self._display_mapping.get(mode, mode.title()) for mode in fan_modes
        ]

        # Reverse mapping: display_name → internal_mode
        self._display_to_internal: dict[str, str] = {}
        for mode in fan_modes:
            display = self._display_mapping.get(mode, mode.title())
            self._display_to_internal[display] = mode

        # Track last user selection for virtual mode sync
        self._last_ha_selection: str | None = None

        self._attr_unique_id = f"ipmi_{host_name}_fan_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

    @property
    def current_option(self) -> str | None:
        """Return the current fan mode as a display name."""
        if self.coordinator.data is None:
            return None

        actual_mode = self.coordinator.data.get("fan_mode")
        if actual_mode is None:
            return None

        return self._get_display_mode_for_sync(actual_mode)

    async def async_select_option(self, option: str) -> None:
        """Set the fan mode."""
        internal_mode = self._display_to_internal.get(option)
        if internal_mode is None:
            raise HomeAssistantError(f"Unknown fan mode: {option}")

        # Resolve virtual mode to actual IPMI mode
        actual_mode = self._virtual_mode_mapping.get(internal_mode, internal_mode)

        # Track the user's selection (may be virtual)
        self._last_ha_selection = option

        try:
            result = await self.hass.async_add_executor_job(
                self._client.set_fan_mode, internal_mode
            )
        except IpmiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err

        if not result:
            raise HomeAssistantError(f"Failed to set fan mode to {option}")

        await self.coordinator.async_request_refresh()

    def _get_display_mode_for_sync(self, actual_internal_mode: str) -> str:
        """Determine the correct display mode considering virtual modes.

        If a virtual mode was last selected and maps to the current actual mode,
        show the virtual mode. Otherwise show the direct mapping.
        """
        if self._last_ha_selection:
            last_internal = self._display_to_internal.get(self._last_ha_selection)
            if last_internal:
                # Check if last selection (possibly virtual) maps to current actual
                mapped = self._virtual_mode_mapping.get(last_internal, last_internal)
                if mapped == actual_internal_mode:
                    return self._last_ha_selection

        # Check if any virtual mode maps to the actual mode
        for virtual_mode, maps_to in self._virtual_mode_mapping.items():
            if maps_to == actual_internal_mode:
                display = self._display_mapping.get(virtual_mode, virtual_mode.title())
                if self._last_ha_selection == display:
                    return display

        # Default: direct display mapping
        return self._display_mapping.get(
            actual_internal_mode, actual_internal_mode.title()
        )
