"""The IPMI Controller integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .button import async_execute_bmc_cold_reset
from .const import (
    CONF_ADDON_URL,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_IPMI_IP,
    CONF_PASSWORD,
    CONF_POWER_CONTROL,
    CONF_PRIVILEGE_LEVEL,
    CONF_SENSORS,
    CONF_USERNAME,
    DEFAULT_POWER_CONTROL,
    DOMAIN,
    migrate_power_control,
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


def _entry_for_entity(
    hass: HomeAssistant, entity_id: str
) -> tuple[ConfigEntry, dict]:
    """Resolve the IPMI config entry (and its runtime data) owning an entity."""
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if (
        entity_entry is None
        or entity_entry.config_entry_id not in hass.data.get(DOMAIN, {})
    ):
        raise HomeAssistantError(f"No IPMI config entry found for entity {entity_id}")

    entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
    if entry is None:
        raise HomeAssistantError(f"No IPMI config entry found for entity {entity_id}")

    return entry, hass.data[DOMAIN][entity_entry.config_entry_id]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current version."""
    _LOGGER.debug("Migrating IPMI Controller entry from version %s", entry.version)

    if entry.version > 3:
        # This version of the integration cannot migrate an entry that was
        # created by a newer version.
        return False

    if entry.version < 3:
        new_options = dict(entry.options)
        new_options[CONF_POWER_CONTROL] = migrate_power_control(
            new_options.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)
        )
        hass.config_entries.async_update_entry(
            entry, options=new_options, version=3
        )

    _LOGGER.debug("Migration of IPMI Controller entry to version 3 successful")
    return True


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
        "bmc_reset_armed": False,
        # monotonic deadline; while in the future the coordinator treats
        # connection failures as the BMC rebooting rather than as errors
        "bmc_reset_grace_until": 0.0,
        # Snapshot taken after the first refresh, so units the coordinator just
        # learned are already baked in and do not read as a user-made change.
        "options_fingerprint": _options_fingerprint(entry),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register the force_power_off service (once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_POWER_OFF):
        async def handle_force_power_off(call: ServiceCall) -> None:
            """Handle the force_power_off service call."""
            confirm = call.data["confirm"]
            _target_entry, entry_data = _entry_for_entity(hass, call.data["entity_id"])
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

    if not hass.services.has_service(DOMAIN, SERVICE_BMC_COLD_RESET):
        async def handle_bmc_cold_reset(call: ServiceCall) -> None:
            """Handle the bmc_cold_reset service call."""
            confirm = call.data["confirm"]
            target_entry, entry_data = _entry_for_entity(
                hass, call.data["entity_id"]
            )

            # The entity-level gate is privilege, so the service must enforce it
            # too — otherwise a service call is a way around it.
            if target_entry.data.get(CONF_PRIVILEGE_LEVEL) != "ADMINISTRATOR":
                raise HomeAssistantError(
                    "BMC cold reset requires Administrator credentials"
                )

            if confirm:
                entry_data["bmc_reset_armed"] = True

            await async_execute_bmc_cold_reset(
                hass, target_entry, entry_data["client"]
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_BMC_COLD_RESET,
            handle_bmc_cold_reset,
            schema=DESTRUCTIVE_SERVICE_SCHEMA,
        )

    return True


def _options_fingerprint(entry: ConfigEntry) -> dict:
    """Return the entry options with per-sensor units stripped out.

    The coordinator writes units it learns from live readings back into the options.
    Those writes must not trigger a reload, so they are excluded from the comparison
    that decides whether a reload is needed.
    """
    options = dict(entry.options)
    sensors = options.get(CONF_SENSORS)
    if isinstance(sensors, list):
        options[CONF_SENSORS] = [
            {k: v for k, v in sensor.items() if k != "unit"}
            if isinstance(sensor, dict)
            else sensor
            for sensor in sensors
        ]
    return options


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change, ignoring self-healed sensor units."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    fingerprint = _options_fingerprint(entry)

    if entry_data is not None:
        if entry_data.get("options_fingerprint") == fingerprint:
            _LOGGER.debug("Options changed only by learned sensor units; not reloading")
            return
        entry_data["options_fingerprint"] = fingerprint

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an IPMI Controller config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Unregister services if no more entries
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_FORCE_POWER_OFF)
            hass.services.async_remove(DOMAIN, SERVICE_BMC_COLD_RESET)
    return unload_ok
