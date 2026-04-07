"""The IPMI Controller integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_ADMIN_PASS,
    CONF_ADMIN_USER,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_IPMI_IP,
    CONF_OPERATOR_PASS,
    CONF_OPERATOR_USER,
    DOMAIN,
)
from .coordinator import IpmiDataUpdateCoordinator
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IPMI Controller from a config entry."""
    # Build fan config from options for the IpmiClient
    fan_config = {}
    if entry.options.get(CONF_FAN_MODE_QUERY_COMMAND):
        fan_config["fan_mode_query_command"] = entry.options[CONF_FAN_MODE_QUERY_COMMAND]
        fan_config["fan_mode_response_mapping"] = {
            # Keys may be stored as strings in JSON; convert back to int
            (int(k) if isinstance(k, str) else k): v
            for k, v in entry.options.get(CONF_FAN_MODE_RESPONSE_MAPPING, {}).items()
        }
        fan_config["fan_mode_commands"] = entry.options.get(CONF_FAN_MODE_COMMANDS, {})

    client = IpmiClient(
        ip=entry.data[CONF_IPMI_IP],
        operator_user=entry.data[CONF_OPERATOR_USER],
        operator_pass=entry.data[CONF_OPERATOR_PASS],
        admin_user=entry.data[CONF_ADMIN_USER],
        admin_pass=entry.data[CONF_ADMIN_PASS],
        fan_config=fan_config,
    )

    # Verify connectivity
    try:
        await hass.async_add_executor_job(client.get_chassis_status)
    except IpmiAuthError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except IpmiConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = IpmiDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload on options change
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an IPMI Controller config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
