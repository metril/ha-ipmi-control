"""Diagnostics for IPMI Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = dict(entry.data)
    # Redact credentials
    for key in (CONF_USERNAME, CONF_PASSWORD):
        if key in data:
            data[key] = REDACTED

    coordinator_data = None
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        coordinator = hass.data[DOMAIN][entry.entry_id].get("coordinator")
        if coordinator and coordinator.data:
            coordinator_data = coordinator.data

    return {
        "config_entry_data": data,
        "config_entry_options": dict(entry.options),
        "coordinator_data": coordinator_data,
    }
