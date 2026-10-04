"""Diagnostics for IPMI Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_ADDON_URL, CONF_IPMI_IP
from .data import IpmiConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_IPMI_IP, CONF_ADDON_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IpmiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "coordinator_data": async_redact_data(coordinator.data, TO_REDACT)
        if coordinator.data is not None
        else None,
        "last_update_success": coordinator.last_update_success,
    }
