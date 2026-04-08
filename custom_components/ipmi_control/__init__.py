"""The IPMI Controller integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADDON_URL,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_IPMI_IP,
    CONF_PASSWORD,
    CONF_PRIVILEGE_LEVEL,
    CONF_USERNAME,
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

SERVICE_FORCE_POWER_OFF = "force_power_off"
SERVICE_FORCE_POWER_OFF_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.string,
        vol.Required("confirm"): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IPMI Controller from a config entry."""
    session = async_get_clientsession(hass)

    # Build fan config from options
    fan_config = {}
    if entry.options.get(CONF_FAN_MODE_QUERY_COMMAND):
        fan_config["fan_mode_query_command"] = entry.options[CONF_FAN_MODE_QUERY_COMMAND]
        fan_config["fan_mode_response_mapping"] = {
            (int(k) if isinstance(k, str) else k): v
            for k, v in entry.options.get(CONF_FAN_MODE_RESPONSE_MAPPING, {}).items()
        }
        fan_config["fan_mode_commands"] = entry.options.get(CONF_FAN_MODE_COMMANDS, {})

    client = IpmiClient(
        session=session,
        addon_url=entry.data[CONF_ADDON_URL],
        host_ip=entry.data[CONF_IPMI_IP],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        privilege_level=entry.data[CONF_PRIVILEGE_LEVEL],
        fan_config=fan_config,
    )

    # Verify add-on is reachable
    try:
        await client.check_addon_health()
    except IpmiConnectionError as err:
        raise ConfigEntryNotReady(
            f"IPMI add-on not reachable: {err}"
        ) from err

    coordinator = IpmiDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
        "hard_off_armed": False,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register the force_power_off service (once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_POWER_OFF):
        async def handle_force_power_off(call: ServiceCall) -> None:
            """Handle the force_power_off service call."""
            entity_id = call.data["entity_id"]
            confirm = call.data["confirm"]

            # Find the config entry that owns this entity
            target_entry_id: str | None = None
            for eid, edata in hass.data.get(DOMAIN, {}).items():
                if isinstance(edata, dict) and "client" in edata:
                    target_entry_id = eid
                    # Match by checking entity_id against expected pattern
                    entry_obj = hass.config_entries.async_get_entry(eid)
                    if entry_obj:
                        from .const import CONF_HOST_NAME
                        host_name = entry_obj.data.get(CONF_HOST_NAME, "")
                        expected_entity = f"button.ipmi_{host_name}_force_hard_off"
                        if entity_id == expected_entity:
                            break
            else:
                # If no exact match found, use the last one checked
                pass

            if target_entry_id is None:
                raise HomeAssistantError(
                    f"No IPMI config entry found for entity {entity_id}"
                )

            entry_data = hass.data[DOMAIN][target_entry_id]
            target_client: IpmiClient = entry_data["client"]

            if confirm:
                # Direct execution: arm, fire, disarm
                entry_data["hard_off_armed"] = True
                try:
                    await target_client.hard_power_off()
                except (IpmiAuthError, IpmiConnectionError) as err:
                    raise HomeAssistantError(str(err)) from err
                finally:
                    entry_data["hard_off_armed"] = False
            else:
                # Requires pre-arming via the switch
                if not entry_data.get("hard_off_armed", False):
                    raise HomeAssistantError("Force power off is not armed")
                try:
                    await target_client.hard_power_off()
                except (IpmiAuthError, IpmiConnectionError) as err:
                    raise HomeAssistantError(str(err)) from err
                finally:
                    entry_data["hard_off_armed"] = False

        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_POWER_OFF,
            handle_force_power_off,
            schema=SERVICE_FORCE_POWER_OFF_SCHEMA,
        )

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an IPMI Controller config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Unregister service if no more entries
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_FORCE_POWER_OFF)
    return unload_ok
