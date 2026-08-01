"""Config flow for IPMI Controller integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ADDON_URL,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_DISPLAY_MAPPING,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_FAN_MODES,
    CONF_HARD_OFF_DISARM_TIMEOUT,
    CONF_HOST_NAME,
    CONF_IPMI_IP,
    CONF_MOTHERBOARD,
    CONF_PASSWORD,
    CONF_POWER_CONTROL,
    CONF_PRIVILEGE_LEVEL,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_SENSORS,
    CONF_SENSORS,
    CONF_THRESH_LC,
    CONF_THRESH_LNC,
    CONF_THRESH_LNR,
    CONF_THRESH_UC,
    CONF_THRESH_UNC,
    CONF_THRESH_UNR,
    CONF_THRESHOLD_SENSORS,
    CONF_USERNAME,
    CONF_VIRTUAL_MODE_MAPPING,
    DEFAULT_HARD_OFF_DISARM_TIMEOUT,
    DEFAULT_POWER_CONTROL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    CONF_POWER_STATE_HOLD,
    DEFAULT_POWER_STATE_HOLD,
    MOTHERBOARD_NONE,
    MOTHERBOARD_PROFILES,
    POWER_HARD_OFF,
    POWER_ON,
    POWER_SOFT_OFF,
    migrate_power_control,
)

CONF_MANUAL_SENSORS = "manual_sensors"
DEFAULT_ADDON_PORT = 8099
DEFAULT_ADDON_URL = f"http://02ae3471-ipmi-control:{DEFAULT_ADDON_PORT}"

PRIVILEGE_LEVELS = ["ADMINISTRATOR", "OPERATOR"]

# Virtual fan mode management (options flow only)
CONF_VIRTUAL_MODE_ACTION = "virtual_mode_action"
CONF_VIRTUAL_MODE_INTERNAL_NAME = "virtual_mode_internal_name"
CONF_VIRTUAL_MODE_DISPLAY_NAME = "virtual_mode_display_name"
CONF_VIRTUAL_MODE_MAPS_TO = "virtual_mode_maps_to"
CONF_VIRTUAL_MODE_COMMANDS = "virtual_mode_commands"

VIRTUAL_MODE_ACTION_ADD = "add"
VIRTUAL_MODE_ACTION_DONE = "done"
VIRTUAL_MODE_ACTION_EDIT_PREFIX = "edit:"
VIRTUAL_MODE_ACTION_REMOVE_PREFIX = "remove:"

INTERNAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)

POWER_CONTROL_SELECT_OPTIONS = [
    SelectOptionDict(value=POWER_ON, label="Power On"),
    SelectOptionDict(value=POWER_SOFT_OFF, label="Soft Shutdown"),
    SelectOptionDict(value=POWER_HARD_OFF, label="Hard Power Off"),
]

MOTHERBOARD_OPTIONS = [MOTHERBOARD_NONE] + list(MOTHERBOARD_PROFILES.keys())


def _build_profile_options(
    motherboard: str, existing_options: dict[str, Any]
) -> dict[str, Any]:
    """Build the fan mode option keys for a motherboard profile.

    Any user-defined virtual modes found in ``existing_options`` are carried
    over so that (re)selecting a motherboard profile never silently wipes
    them out.
    """
    profile = MOTHERBOARD_PROFILES[motherboard]
    fan_modes = list(profile["fan_modes"])
    display_mapping = dict(profile["fan_mode_display_mapping"])
    commands = {k: list(v) for k, v in profile["fan_mode_commands"].items()}
    virtual_mapping: dict[str, str] = {}

    existing_virtual_mapping = existing_options.get(CONF_VIRTUAL_MODE_MAPPING, {})
    existing_display = existing_options.get(CONF_FAN_MODE_DISPLAY_MAPPING, {})
    existing_commands = existing_options.get(CONF_FAN_MODE_COMMANDS, {})
    existing_fan_modes = existing_options.get(CONF_FAN_MODES, [])

    for virtual_name, maps_to in existing_virtual_mapping.items():
        if virtual_name not in existing_fan_modes:
            continue  # stale entry, drop it
        fan_modes.append(virtual_name)
        display_mapping[virtual_name] = existing_display.get(
            virtual_name, virtual_name.title()
        )
        commands[virtual_name] = existing_commands.get(virtual_name, [])
        virtual_mapping[virtual_name] = maps_to

    return {
        CONF_FAN_MODES: fan_modes,
        CONF_FAN_MODE_DISPLAY_MAPPING: display_mapping,
        CONF_FAN_MODE_QUERY_COMMAND: profile["fan_mode_query_command"],
        CONF_FAN_MODE_RESPONSE_MAPPING: profile["fan_mode_response_mapping"],
        CONF_FAN_MODE_COMMANDS: commands,
        CONF_VIRTUAL_MODE_MAPPING: virtual_mapping,
    }


def _sensor_option_label(sensor: dict[str, str]) -> str:
    """Build the picker label for a discovered SDR sensor.

    Shows the unit when the BMC reported one. Sensors sitting at "ns" report nothing
    at all — typically unpopulated DIMM slots or absent drive bays — so they are
    called out rather than silently offered as ordinary choices.
    """
    name = sensor["name"]
    unit = sensor.get("unit")
    if unit:
        return f"{name} ({unit})"
    if sensor.get("status") == "ns":
        return f"{name} (no reading)"
    return name


def _format_virtual_mode_commands(commands: list[dict[str, Any]]) -> str:
    """Format parsed IPMI command dicts back into raw hex text for editing."""
    lines = []
    for cmd in commands:
        byte_values = [cmd["netfn"], cmd["command"], *cmd.get("data", [])]
        lines.append(" ".join(f"0x{b:02x}" for b in byte_values))
    return "\n".join(lines)


def _parse_virtual_mode_commands(
    text: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Parse raw IPMI command text into command dicts.

    Each non-empty line must be whitespace-separated hex byte tokens, e.g.
    ``0x30 0x45 0x01 0x00``: the first byte is netfn, the second is command,
    and any remaining bytes are data. Returns ``(commands, error_key)`` where
    ``error_key`` is ``None`` on success (and ``commands`` is ``[]`` on
    failure).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return [], "commands_empty"

    commands: list[dict[str, Any]] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) < 2:
            return [], "commands_too_short"

        byte_values: list[int] = []
        for token in tokens:
            if not token.lower().startswith("0x"):
                return [], "commands_invalid_hex"
            try:
                value = int(token, 16)
            except ValueError:
                return [], "commands_invalid_hex"
            if not 0x00 <= value <= 0xFF:
                return [], "commands_byte_range"
            byte_values.append(value)

        commands.append(
            {
                "netfn": byte_values[0],
                "command": byte_values[1],
                "data": byte_values[2:],
            }
        )

    return commands, None


class IpmiControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for IPMI Controller."""

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._client: IpmiClient | None = None
        self._sdr_units: dict[str, str] = {}

    def _get_client(self) -> IpmiClient:
        """Get or create an IpmiClient from collected data."""
        if self._client is None:
            session = async_get_clientsession(self.hass)
            self._client = IpmiClient(
                session=session,
                addon_url=self._data[CONF_ADDON_URL],
                host_ip=self._data[CONF_IPMI_IP],
                username=self._data[CONF_USERNAME],
                password=self._data[CONF_PASSWORD],
                privilege_level=self._data[CONF_PRIVILEGE_LEVEL],
            )
        return self._client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: IPMI connection credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Guard against configuring the same physical BMC twice under two
            # different host names — uniqueness below is keyed on the
            # user-typed host name, not the BMC address.
            for entry in self._async_current_entries():
                if entry.data.get(CONF_IPMI_IP) == user_input[CONF_IPMI_IP]:
                    return self.async_abort(reason="already_configured")

            session = async_get_clientsession(self.hass)
            addon_url = user_input[CONF_ADDON_URL]

            try:
                await IpmiClient.test_addon_connection(session, addon_url)
            except IpmiConnectionError:
                errors["base"] = "addon_not_reachable"

            if not errors:
                try:
                    await IpmiClient.test_ipmi_connection(
                        session,
                        addon_url,
                        user_input[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "invalid_auth"
                except IpmiConnectionError:
                    errors["base"] = "cannot_connect"

            if not errors and user_input[CONF_PRIVILEGE_LEVEL] == "ADMINISTRATOR":
                try:
                    await IpmiClient.test_admin_privilege(
                        session,
                        addon_url,
                        user_input[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "insufficient_privilege"
                except IpmiConnectionError:
                    pass  # connectivity already verified above

            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST_NAME])
                self._abort_if_unique_id_configured()

                self._data = user_input
                self._client = None
                return await self.async_step_power()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST_NAME): str,
                    vol.Required(CONF_IPMI_IP): str,
                    vol.Required(CONF_USERNAME, default="Administrator"): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_PRIVILEGE_LEVEL, default="ADMINISTRATOR"
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value="ADMINISTRATOR", label="Administrator"),
                                SelectOptionDict(value="OPERATOR", label="Operator"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_ADDON_URL, default=DEFAULT_ADDON_URL): str,
                }
            ),
            errors=errors,
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Power control policy and scan interval."""
        if user_input is not None:
            self._options[CONF_POWER_CONTROL] = user_input[CONF_POWER_CONTROL]
            self._options[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            self._options[CONF_POWER_STATE_HOLD] = user_input[CONF_POWER_STATE_HOLD]
            self._options[CONF_HARD_OFF_DISARM_TIMEOUT] = user_input[CONF_HARD_OFF_DISARM_TIMEOUT]
            return await self.async_step_fan_profile()

        return self.async_show_form(
            step_id="power",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_CONTROL, default=DEFAULT_POWER_CONTROL
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=POWER_CONTROL_SELECT_OPTIONS,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_POWER_STATE_HOLD, default=DEFAULT_POWER_STATE_HOLD
                    ): vol.All(int, vol.Range(min=0, max=300)),
                    vol.Required(
                        CONF_HARD_OFF_DISARM_TIMEOUT, default=DEFAULT_HARD_OFF_DISARM_TIMEOUT
                    ): vol.All(int, vol.Range(min=5, max=300)),
                }
            ),
        )

    async def async_step_fan_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Select motherboard profile for fan control."""
        if user_input is not None:
            motherboard = user_input[CONF_MOTHERBOARD]
            self._options[CONF_MOTHERBOARD] = motherboard

            if motherboard != MOTHERBOARD_NONE and motherboard in MOTHERBOARD_PROFILES:
                # Fresh entries never have pre-existing virtual modes, but
                # route through the shared builder for consistency with the
                # options flow's rebuild logic.
                self._options.update(_build_profile_options(motherboard, self._options))

            return await self.async_step_sensor_select()

        return self.async_show_form(
            step_id="fan_profile",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MOTHERBOARD, default=MOTHERBOARD_NONE
                    ): vol.In(MOTHERBOARD_OPTIONS),
                }
            ),
        )

    async def async_step_sensor_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Select BMC sensors to expose."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_SENSORS, [])
            manual = user_input.get(CONF_MANUAL_SENSORS, "").strip()

            if manual:
                for name in manual.split(","):
                    name = name.strip()
                    if name and name not in selected:
                        selected.append(name)

            if selected:
                # Build sensor entries — look up units from SDR data if available
                sensor_entries = []
                for name in selected:
                    entry = {"name": name, "unit": self._sdr_units.get(name, "")}
                    sensor_entries.append(entry)
                self._options[CONF_SENSORS] = sensor_entries
            else:
                self._options[CONF_SENSORS] = []

            return self._create_entry()

        # Query SDR for all sensors
        client = self._get_client()
        sdr_error = False
        sdr_sensors: list[dict[str, str]] = []
        try:
            sdr_sensors = await client.get_sdr_list()
        except Exception:
            _LOGGER.exception("Failed to query SDR sensors")
            sdr_error = True

        # Store unit mapping for later
        self._sdr_units: dict[str, str] = {s["name"]: s["unit"] for s in sdr_sensors}

        schema_fields: dict[Any, Any] = {}

        if sdr_sensors:
            sensor_options = [
                SelectOptionDict(value=s["name"], label=_sensor_option_label(s))
                for s in sdr_sensors
            ]
            schema_fields[vol.Optional(CONF_SELECTED_SENSORS)] = SelectSelector(
                SelectSelectorConfig(
                    options=sensor_options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        schema_fields[vol.Optional(CONF_MANUAL_SENSORS, default="")] = str

        if sdr_error:
            errors["base"] = "sdr_query_failed"

        return self.async_show_form(
            step_id="sensor_select",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"IPMI {self._data[CONF_HOST_NAME].title()}",
            data=self._data,
            options=self._options,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                await IpmiClient.test_ipmi_connection(
                    session,
                    reauth_entry.data[CONF_ADDON_URL],
                    reauth_entry.data[CONF_IPMI_IP],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except IpmiAuthError:
                errors["base"] = "invalid_auth"
            except IpmiConnectionError:
                errors["base"] = "cannot_connect"

            if not errors and user_input[CONF_PRIVILEGE_LEVEL] == "ADMINISTRATOR":
                try:
                    await IpmiClient.test_admin_privilege(
                        session,
                        reauth_entry.data[CONF_ADDON_URL],
                        reauth_entry.data[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "insufficient_privilege"
                except IpmiConnectionError:
                    pass

            if not errors:
                updated_data = {**reauth_entry.data, **user_input}
                return self.async_update_reload_and_abort(
                    reauth_entry, data=updated_data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_PRIVILEGE_LEVEL,
                        default=reauth_entry.data.get(CONF_PRIVILEGE_LEVEL, "ADMINISTRATOR"),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value="ADMINISTRATOR", label="Administrator"),
                                SelectOptionDict(value="OPERATOR", label="Operator"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            # Same duplicate-BMC guard as async_step_user, but exclude the
            # entry being reconfigured — comparing it against itself must
            # not trip the guard.
            for entry in self._async_current_entries():
                if (
                    entry.entry_id != reconfigure_entry.entry_id
                    and entry.data.get(CONF_IPMI_IP) == user_input[CONF_IPMI_IP]
                ):
                    return self.async_abort(reason="already_configured")

            session = async_get_clientsession(self.hass)
            addon_url = user_input[CONF_ADDON_URL]

            try:
                await IpmiClient.test_addon_connection(session, addon_url)
            except IpmiConnectionError:
                errors["base"] = "addon_not_reachable"

            if not errors:
                try:
                    await IpmiClient.test_ipmi_connection(
                        session,
                        addon_url,
                        user_input[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "invalid_auth"
                except IpmiConnectionError:
                    errors["base"] = "cannot_connect"

            if not errors and user_input[CONF_PRIVILEGE_LEVEL] == "ADMINISTRATOR":
                try:
                    await IpmiClient.test_admin_privilege(
                        session,
                        addon_url,
                        user_input[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "insufficient_privilege"
                except IpmiConnectionError:
                    pass

            if not errors:
                # Preserve host name from original entry
                user_input[CONF_HOST_NAME] = reconfigure_entry.data[CONF_HOST_NAME]
                return self.async_update_reload_and_abort(
                    reconfigure_entry, data=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IPMI_IP,
                        default=reconfigure_entry.data[CONF_IPMI_IP],
                    ): str,
                    vol.Required(
                        CONF_USERNAME,
                        default=reconfigure_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_PRIVILEGE_LEVEL,
                        default=reconfigure_entry.data.get(CONF_PRIVILEGE_LEVEL, "ADMINISTRATOR"),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value="ADMINISTRATOR", label="Administrator"),
                                SelectOptionDict(value="OPERATOR", label="Operator"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_ADDON_URL,
                        default=reconfigure_entry.data[CONF_ADDON_URL],
                    ): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> IpmiControllerOptionsFlow:
        """Get the options flow handler."""
        return IpmiControllerOptionsFlow(config_entry)


class IpmiControllerOptionsFlow(OptionsFlow):
    """Handle options flow for IPMI Controller."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._new_options: dict[str, Any] = {}
        self._client: IpmiClient | None = None
        self._selected_threshold_sensors: list[str] = []
        self._threshold_index: int = 0
        self._sdr_units: dict[str, str] = {}
        # Virtual fan mode management
        self._real_fan_modes: list[str] = []
        self._editing_virtual_mode: str | None = None

    def _get_client(self) -> IpmiClient:
        """Get or create an IpmiClient from config entry data."""
        if self._client is None:
            data = self._config_entry.data
            session = async_get_clientsession(self.hass)
            self._client = IpmiClient(
                session=session,
                addon_url=data[CONF_ADDON_URL],
                host_ip=data[CONF_IPMI_IP],
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                privilege_level=data[CONF_PRIVILEGE_LEVEL],
            )
        return self._client

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            motherboard = user_input.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE)
            self._new_options = {**self._config_entry.options, **user_input}

            if motherboard != MOTHERBOARD_NONE and motherboard in MOTHERBOARD_PROFILES:
                # Rebuild from the profile, preserving any existing
                # user-defined virtual modes rather than wiping them out.
                self._new_options.update(
                    _build_profile_options(motherboard, self._config_entry.options)
                )
                self._real_fan_modes = list(
                    MOTHERBOARD_PROFILES[motherboard]["fan_modes"]
                )
                return await self.async_step_virtual_modes()

            # Motherboard set to "none": drop every fan-mode key. Without this
            # the previous profile's config survives in the spread above, and
            # select.py keys off CONF_FAN_MODES (not CONF_MOTHERBOARD), so the
            # Fan Mode entity would keep issuing raw commands to the BMC after
            # the user disabled fan control.
            for fan_key in (
                CONF_FAN_MODES,
                CONF_FAN_MODE_DISPLAY_MAPPING,
                CONF_FAN_MODE_COMMANDS,
                CONF_FAN_MODE_QUERY_COMMAND,
                CONF_FAN_MODE_RESPONSE_MAPPING,
                CONF_VIRTUAL_MODE_MAPPING,
            ):
                self._new_options.pop(fan_key, None)

            return await self.async_step_sensor_select()

        current_opts = self._config_entry.options
        current_power = migrate_power_control(
            current_opts.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL)
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_CONTROL,
                        default=current_power,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=POWER_CONTROL_SELECT_OPTIONS,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_POWER_STATE_HOLD,
                        default=current_opts.get(CONF_POWER_STATE_HOLD, DEFAULT_POWER_STATE_HOLD),
                    ): vol.All(int, vol.Range(min=0, max=300)),
                    vol.Required(
                        CONF_HARD_OFF_DISARM_TIMEOUT,
                        default=current_opts.get(CONF_HARD_OFF_DISARM_TIMEOUT, DEFAULT_HARD_OFF_DISARM_TIMEOUT),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_MOTHERBOARD,
                        default=current_opts.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE),
                    ): vol.In(MOTHERBOARD_OPTIONS),
                }
            ),
        )

    def _remove_virtual_mode(self, internal_name: str) -> None:
        """Remove a virtual mode from all four option keys it is merged into."""
        self._new_options.get(CONF_VIRTUAL_MODE_MAPPING, {}).pop(internal_name, None)
        self._new_options.get(CONF_FAN_MODE_DISPLAY_MAPPING, {}).pop(internal_name, None)
        self._new_options.get(CONF_FAN_MODE_COMMANDS, {}).pop(internal_name, None)
        fan_modes = self._new_options.get(CONF_FAN_MODES, [])
        if internal_name in fan_modes:
            fan_modes.remove(internal_name)

    async def async_step_virtual_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List existing virtual fan modes and let the user add/edit/remove them.

        A virtual mode is a fan mode the BMC has no concept of — the user
        builds it from a raw IPMI command sequence, and it reports back as
        whichever real mode it is mapped to.
        """
        if user_input is not None:
            action = user_input[CONF_VIRTUAL_MODE_ACTION]

            if action == VIRTUAL_MODE_ACTION_DONE:
                return await self.async_step_sensor_select()

            if action == VIRTUAL_MODE_ACTION_ADD:
                self._editing_virtual_mode = None
                return await self.async_step_virtual_mode_edit()

            if action.startswith(VIRTUAL_MODE_ACTION_EDIT_PREFIX):
                self._editing_virtual_mode = action[
                    len(VIRTUAL_MODE_ACTION_EDIT_PREFIX):
                ]
                return await self.async_step_virtual_mode_edit()

            if action.startswith(VIRTUAL_MODE_ACTION_REMOVE_PREFIX):
                self._remove_virtual_mode(
                    action[len(VIRTUAL_MODE_ACTION_REMOVE_PREFIX):]
                )
                return await self.async_step_virtual_modes()

        virtual_modes = self._new_options.get(CONF_VIRTUAL_MODE_MAPPING, {})
        display_mapping = self._new_options.get(CONF_FAN_MODE_DISPLAY_MAPPING, {})

        action_options = [
            SelectOptionDict(
                value=VIRTUAL_MODE_ACTION_ADD, label="Add a new virtual mode"
            ),
        ]
        for internal_name in virtual_modes:
            display = display_mapping.get(internal_name, internal_name.title())
            action_options.append(
                SelectOptionDict(
                    value=f"{VIRTUAL_MODE_ACTION_EDIT_PREFIX}{internal_name}",
                    label=f"Edit '{display}'",
                )
            )
            action_options.append(
                SelectOptionDict(
                    value=f"{VIRTUAL_MODE_ACTION_REMOVE_PREFIX}{internal_name}",
                    label=f"Remove '{display}'",
                )
            )
        action_options.append(
            SelectOptionDict(value=VIRTUAL_MODE_ACTION_DONE, label="Done")
        )

        return self.async_show_form(
            step_id="virtual_modes",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VIRTUAL_MODE_ACTION, default=VIRTUAL_MODE_ACTION_DONE
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=action_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={"virtual_mode_count": str(len(virtual_modes))},
        )

    async def async_step_virtual_mode_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or edit a single virtual fan mode."""
        errors: dict[str, str] = {}
        editing = self._editing_virtual_mode

        virtual_modes = self._new_options.get(CONF_VIRTUAL_MODE_MAPPING, {})
        display_mapping = self._new_options.get(CONF_FAN_MODE_DISPLAY_MAPPING, {})
        commands_mapping = self._new_options.get(CONF_FAN_MODE_COMMANDS, {})
        fan_modes = self._new_options.get(CONF_FAN_MODES, [])

        if user_input is not None:
            internal_name = user_input[CONF_VIRTUAL_MODE_INTERNAL_NAME].strip().lower()
            display_name = user_input[CONF_VIRTUAL_MODE_DISPLAY_NAME].strip()
            maps_to = user_input[CONF_VIRTUAL_MODE_MAPS_TO]
            commands_text = user_input[CONF_VIRTUAL_MODE_COMMANDS]

            if not INTERNAL_NAME_RE.match(internal_name):
                errors[CONF_VIRTUAL_MODE_INTERNAL_NAME] = "invalid_internal_name"
            elif internal_name in self._real_fan_modes or (
                internal_name in virtual_modes and internal_name != editing
            ):
                errors[CONF_VIRTUAL_MODE_INTERNAL_NAME] = "duplicate_internal_name"

            if not display_name:
                errors[CONF_VIRTUAL_MODE_DISPLAY_NAME] = "display_name_required"

            parsed_commands, command_error = _parse_virtual_mode_commands(
                commands_text
            )
            if command_error:
                errors[CONF_VIRTUAL_MODE_COMMANDS] = command_error

            if not errors:
                if editing and editing != internal_name:
                    # Renamed: drop the old entry before writing the new one.
                    virtual_modes.pop(editing, None)
                    display_mapping.pop(editing, None)
                    commands_mapping.pop(editing, None)
                    if editing in fan_modes:
                        fan_modes.remove(editing)

                if internal_name not in fan_modes:
                    fan_modes.append(internal_name)
                display_mapping[internal_name] = display_name
                commands_mapping[internal_name] = parsed_commands
                virtual_modes[internal_name] = maps_to

                self._new_options[CONF_FAN_MODES] = fan_modes
                self._new_options[CONF_FAN_MODE_DISPLAY_MAPPING] = display_mapping
                self._new_options[CONF_FAN_MODE_COMMANDS] = commands_mapping
                self._new_options[CONF_VIRTUAL_MODE_MAPPING] = virtual_modes

                self._editing_virtual_mode = None
                return await self.async_step_virtual_modes()

        default_maps_to = self._real_fan_modes[0] if self._real_fan_modes else ""
        if editing:
            defaults = {
                CONF_VIRTUAL_MODE_INTERNAL_NAME: editing,
                CONF_VIRTUAL_MODE_DISPLAY_NAME: display_mapping.get(
                    editing, editing.title()
                ),
                CONF_VIRTUAL_MODE_MAPS_TO: virtual_modes.get(
                    editing, default_maps_to
                ),
                CONF_VIRTUAL_MODE_COMMANDS: _format_virtual_mode_commands(
                    commands_mapping.get(editing, [])
                ),
            }
        else:
            defaults = {
                CONF_VIRTUAL_MODE_INTERNAL_NAME: "",
                CONF_VIRTUAL_MODE_DISPLAY_NAME: "",
                CONF_VIRTUAL_MODE_MAPS_TO: default_maps_to,
                CONF_VIRTUAL_MODE_COMMANDS: "",
            }

        # If the form was resubmitted with errors, keep what the user typed
        # instead of reverting to the stored/blank defaults.
        if user_input is not None:
            defaults.update(
                {
                    key: user_input[key]
                    for key in defaults
                    if key in user_input
                }
            )

        return self.async_show_form(
            step_id="virtual_mode_edit",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VIRTUAL_MODE_INTERNAL_NAME,
                        default=defaults[CONF_VIRTUAL_MODE_INTERNAL_NAME],
                    ): str,
                    vol.Required(
                        CONF_VIRTUAL_MODE_DISPLAY_NAME,
                        default=defaults[CONF_VIRTUAL_MODE_DISPLAY_NAME],
                    ): str,
                    vol.Required(
                        CONF_VIRTUAL_MODE_MAPS_TO,
                        default=defaults[CONF_VIRTUAL_MODE_MAPS_TO],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=mode,
                                    label=display_mapping.get(mode, mode.title()),
                                )
                                for mode in self._real_fan_modes
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_VIRTUAL_MODE_COMMANDS,
                        default=defaults[CONF_VIRTUAL_MODE_COMMANDS],
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
            errors=errors,
        )

    async def async_step_sensor_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select BMC sensors to expose."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_SENSORS, [])
            manual = user_input.get(CONF_MANUAL_SENSORS, "").strip()

            if manual:
                for name in manual.split(","):
                    name = name.strip()
                    if name and name not in selected:
                        selected.append(name)

            if selected:
                # Preserve existing threshold config for sensors that are still selected
                existing_sensors = {
                    s["name"]: s for s in self._config_entry.options.get(CONF_SENSORS, [])
                }
                sensor_entries = []
                for name in selected:
                    existing = existing_sensors.get(name, {})
                    entry: dict[str, Any] = {
                        "name": name,
                        "unit": self._sdr_units.get(name, existing.get("unit", "")),
                    }
                    if existing.get("thresholds"):
                        entry["thresholds"] = existing["thresholds"]
                    sensor_entries.append(entry)
                self._new_options[CONF_SENSORS] = sensor_entries
            else:
                self._new_options[CONF_SENSORS] = []

            # If admin, proceed to threshold configuration
            privilege = self._config_entry.data.get(CONF_PRIVILEGE_LEVEL, "ADMINISTRATOR")
            if selected and privilege == "ADMINISTRATOR":
                return await self.async_step_threshold_sensor_select()

            return self.async_create_entry(title="", data=self._new_options)

        client = self._get_client()
        sdr_error = False
        sdr_sensors: list[dict[str, str]] = []
        try:
            sdr_sensors = await client.get_sdr_list()
        except Exception:
            _LOGGER.exception("Failed to query SDR sensors")
            sdr_error = True

        self._sdr_units = {s["name"]: s["unit"] for s in sdr_sensors}

        schema_fields: dict[Any, Any] = {}

        if sdr_sensors:
            current_sensor_names = [
                s["name"] for s in self._config_entry.options.get(CONF_SENSORS, [])
            ]
            default_selection = [n for n in current_sensor_names if any(s["name"] == n for s in sdr_sensors)]

            sensor_options = [
                SelectOptionDict(value=s["name"], label=_sensor_option_label(s))
                for s in sdr_sensors
            ]
            schema_fields[vol.Optional(
                CONF_SELECTED_SENSORS, default=default_selection
            )] = SelectSelector(
                SelectSelectorConfig(
                    options=sensor_options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        schema_fields[vol.Optional(CONF_MANUAL_SENSORS, default="")] = str

        if sdr_error:
            errors["base"] = "sdr_query_failed"

        return self.async_show_form(
            step_id="sensor_select",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def async_step_threshold_sensor_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select which sensors to configure threshold overrides for."""
        if user_input is not None:
            selected = user_input.get(CONF_THRESHOLD_SENSORS, [])
            if selected:
                self._selected_threshold_sensors = selected
                self._threshold_index = 0
                return await self.async_step_sensor_thresholds()
            return self.async_create_entry(title="", data=self._new_options)

        # Read thresholds from BMC to find which sensors have them
        client = self._get_client()
        sensors = self._new_options.get(CONF_SENSORS, [])
        sensors_with_thresholds: list[str] = []
        for sensor in sensors:
            try:
                thresholds = await client.get_sensor_thresholds(sensor["name"])
                if thresholds:
                    sensors_with_thresholds.append(sensor["name"])
            except Exception:
                _LOGGER.debug("Could not read thresholds for %s", sensor["name"])

        if not sensors_with_thresholds:
            return self.async_create_entry(title="", data=self._new_options)

        # Default to currently configured threshold sensors
        current_threshold_names = [
            s["name"] for s in self._config_entry.options.get(CONF_SENSORS, [])
            if s.get("thresholds")
        ]
        default_selection = [n for n in current_threshold_names if n in sensors_with_thresholds]

        return self.async_show_form(
            step_id="threshold_sensor_select",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_THRESHOLD_SENSORS, default=default_selection
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=name, label=name)
                                for name in sensors_with_thresholds
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_sensor_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure thresholds for each selected sensor."""
        if user_input is not None:
            sensor_name = self._selected_threshold_sensors[self._threshold_index]

            lower = [
                user_input.get(CONF_THRESH_LNR),
                user_input.get(CONF_THRESH_LC),
                user_input.get(CONF_THRESH_LNC),
            ]
            upper = [
                user_input.get(CONF_THRESH_UNC),
                user_input.get(CONF_THRESH_UC),
                user_input.get(CONF_THRESH_UNR),
            ]

            thresholds: dict[str, list[int]] = {}
            if any(v is not None for v in lower):
                thresholds["lower"] = [v or 0 for v in lower]
            if any(v is not None for v in upper):
                thresholds["upper"] = [v or 0 for v in upper]

            # Update the sensor entry in options
            for sensor in self._new_options.get(CONF_SENSORS, []):
                if sensor["name"] == sensor_name:
                    if thresholds:
                        sensor["thresholds"] = thresholds
                    elif "thresholds" in sensor:
                        del sensor["thresholds"]
                    break

            self._threshold_index += 1

            if self._threshold_index < len(self._selected_threshold_sensors):
                return await self.async_step_sensor_thresholds()

            return self.async_create_entry(title="", data=self._new_options)

        sensor_name = self._selected_threshold_sensors[self._threshold_index]
        defaults = await self._read_sensor_thresholds(sensor_name)

        return self.async_show_form(
            step_id="sensor_thresholds",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_THRESH_LNR, default=defaults.get("lnr")): int,
                    vol.Optional(CONF_THRESH_LC, default=defaults.get("lc")): int,
                    vol.Optional(CONF_THRESH_LNC, default=defaults.get("lnc")): int,
                    vol.Optional(CONF_THRESH_UNC, default=defaults.get("unc")): int,
                    vol.Optional(CONF_THRESH_UC, default=defaults.get("uc")): int,
                    vol.Optional(CONF_THRESH_UNR, default=defaults.get("unr")): int,
                }
            ),
            description_placeholders={"sensor_name": sensor_name},
        )

    async def _read_sensor_thresholds(self, sensor_name: str) -> dict[str, int]:
        """Read current thresholds for a sensor from config or BMC."""
        # Check existing config first
        for sensor in self._config_entry.options.get(CONF_SENSORS, []):
            if sensor["name"] == sensor_name:
                thresholds = sensor.get("thresholds", {})
                lower = thresholds.get("lower", [])
                upper = thresholds.get("upper", [])
                result: dict[str, int] = {}
                if len(lower) >= 3:
                    result.update({"lnr": lower[0], "lc": lower[1], "lnc": lower[2]})
                if len(upper) >= 3:
                    result.update({"unc": upper[0], "uc": upper[1], "unr": upper[2]})
                if result:
                    return result

        # Fall back to reading from BMC
        client = self._get_client()
        try:
            thresholds = await client.get_sensor_thresholds(sensor_name)
            if thresholds:
                return thresholds
        except Exception:
            _LOGGER.debug("Could not read thresholds for %s", sensor_name)
        return {}
