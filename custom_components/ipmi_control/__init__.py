"""The IPMI Controller integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .button import async_execute_bmc_cold_reset
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
    signal_disarmed,
)
from .coordinator import IpmiDataUpdateCoordinator
from .data import IpmiConfigEntry, IpmiRuntimeData
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

SERVICE_FORCE_POWER_OFF = "force_power_off"
SERVICE_BMC_COLD_RESET = "bmc_cold_reset"

# Both destructive services take the same shape: the button entity that
# identifies the host, and a confirm flag that bypasses the arm switch.
DESTRUCTIVE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.string,
        vol.Required("confirm"): cv.boolean,
    }
)
SERVICE_FORCE_POWER_OFF_SCHEMA = DESTRUCTIVE_SERVICE_SCHEMA


def _entry_for_entity(hass: HomeAssistant, entity_id: str) -> IpmiConfigEntry:
    """Resolve the loaded IPMI config entry owning an entity."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry is not None and entity_entry.platform == DOMAIN:
        entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
        if entry is not None and entry.state is ConfigEntryState.LOADED:
            return entry
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="entry_not_found",
        translation_placeholders={"entity_id": entity_id},
    )


def _consume_arm(hass: HomeAssistant, entry: IpmiConfigEntry, attr: str) -> None:
    """Clear an arm flag and tell the arm switch to follow."""
    setattr(entry.runtime_data, attr, False)
    async_dispatcher_send(hass, signal_disarmed(entry.entry_id))


async def _handle_force_power_off(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the force_power_off service call."""
    entry = _entry_for_entity(hass, call.data["entity_id"])
    runtime = entry.runtime_data
    if not call.data["confirm"] and not runtime.hard_off_armed:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_armed"
        )
    # Consume the flag before the IPMI call so it can never be reused.
    _consume_arm(hass, entry, "hard_off_armed")
    try:
        await runtime.client.hard_power_off()
    except (IpmiAuthError, IpmiConnectionError) as err:
        raise HomeAssistantError(str(err)) from err


async def _handle_bmc_cold_reset(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the bmc_cold_reset service call."""
    entry = _entry_for_entity(hass, call.data["entity_id"])
    # The entity-level gate is privilege, so the service must enforce it too.
    if entry.data.get(CONF_PRIVILEGE_LEVEL) != "ADMINISTRATOR":
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="requires_admin"
        )
    bypass = call.data["confirm"]
    if not bypass and not entry.runtime_data.bmc_reset_armed:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_armed"
        )
    await async_execute_bmc_cold_reset(
        hass, entry, entry.runtime_data.client, bypass_arm=bypass
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-level services."""

    async def force_power_off(call: ServiceCall) -> None:
        await _handle_force_power_off(hass, call)

    async def bmc_cold_reset(call: ServiceCall) -> None:
        await _handle_bmc_cold_reset(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_POWER_OFF,
        force_power_off,
        schema=SERVICE_FORCE_POWER_OFF_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BMC_COLD_RESET,
        bmc_cold_reset,
        schema=DESTRUCTIVE_SERVICE_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IpmiConfigEntry) -> bool:
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
    entry.runtime_data = IpmiRuntimeData(coordinator=coordinator, client=client)
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: IpmiConfigEntry) -> bool:
    """Unload an IPMI Controller config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
