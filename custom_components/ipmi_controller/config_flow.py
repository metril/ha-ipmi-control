"""Config flow for IPMI Controller integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADMIN_PASS,
    CONF_ADMIN_USER,
    CONF_FAN_LC,
    CONF_FAN_LNC,
    CONF_FAN_LNR,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_DISPLAY_MAPPING,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_FAN_MODES,
    CONF_FAN_UC,
    CONF_FAN_UNC,
    CONF_FAN_UNR,
    CONF_FANS,
    CONF_HOST_NAME,
    CONF_IPMI_IP,
    CONF_MOTHERBOARD,
    CONF_OPERATOR_PASS,
    CONF_OPERATOR_USER,
    CONF_POWER_CONTROL,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_FANS,
    CONF_VIRTUAL_MODE_MAPPING,
    DEFAULT_POWER_CONTROL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MOTHERBOARD_NONE,
    MOTHERBOARD_PROFILES,
    POWER_CONTROL_BOTH,
    POWER_CONTROL_NONE,
    POWER_CONTROL_OFF,
    POWER_CONTROL_ON,
)
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)

POWER_CONTROL_OPTIONS = [
    POWER_CONTROL_BOTH,
    POWER_CONTROL_ON,
    POWER_CONTROL_OFF,
    POWER_CONTROL_NONE,
]

MOTHERBOARD_OPTIONS = [MOTHERBOARD_NONE] + list(MOTHERBOARD_PROFILES.keys())


class IpmiControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for IPMI Controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._client: IpmiClient | None = None
        self._selected_fans: list[str] = []
        self._fan_index: int = 0

    def _get_client(self) -> IpmiClient:
        """Get or create an IpmiClient from collected data."""
        if self._client is None:
            self._client = IpmiClient(
                ip=self._data[CONF_IPMI_IP],
                operator_user=self._data[CONF_OPERATOR_USER],
                operator_pass=self._data[CONF_OPERATOR_PASS],
                admin_user=self._data[CONF_ADMIN_USER],
                admin_pass=self._data[CONF_ADMIN_PASS],
            )
        return self._client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Connection credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._test_connection(
                user_input[CONF_IPMI_IP],
                user_input[CONF_OPERATOR_USER],
                user_input[CONF_OPERATOR_PASS],
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_HOST_NAME])
                self._abort_if_unique_id_configured()

                self._data = user_input
                self._client = None  # Reset client for new credentials
                return await self.async_step_power()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST_NAME): str,
                    vol.Required(CONF_IPMI_IP): str,
                    vol.Required(CONF_OPERATOR_USER, default="HomeAssistant"): str,
                    vol.Required(CONF_OPERATOR_PASS): str,
                    vol.Required(CONF_ADMIN_USER, default="Administrator"): str,
                    vol.Required(CONF_ADMIN_PASS): str,
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
            return await self.async_step_fan_profile()

        return self.async_show_form(
            step_id="power",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_CONTROL, default=DEFAULT_POWER_CONTROL
                    ): vol.In(POWER_CONTROL_OPTIONS),
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
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
                profile = MOTHERBOARD_PROFILES[motherboard]
                self._options[CONF_FAN_MODES] = profile["fan_modes"]
                self._options[CONF_FAN_MODE_DISPLAY_MAPPING] = profile[
                    "fan_mode_display_mapping"
                ]
                self._options[CONF_FAN_MODE_QUERY_COMMAND] = profile[
                    "fan_mode_query_command"
                ]
                self._options[CONF_FAN_MODE_RESPONSE_MAPPING] = profile[
                    "fan_mode_response_mapping"
                ]
                self._options[CONF_FAN_MODE_COMMANDS] = profile["fan_mode_commands"]
                self._options[CONF_VIRTUAL_MODE_MAPPING] = user_input.get(
                    CONF_VIRTUAL_MODE_MAPPING, {}
                )

            return await self.async_step_fan_select()

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

    async def async_step_fan_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Select fan sensors from SDR."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_FANS, [])
            if selected:
                self._selected_fans = selected
                self._fan_index = 0
                return await self.async_step_fan_thresholds()
            else:
                # No fans selected — skip thresholds
                self._options[CONF_FANS] = []
                return self._create_entry()

        # Query SDR for fan sensors
        client = self._get_client()
        try:
            fan_sensors = await self.hass.async_add_executor_job(
                client.get_fan_sensors
            )
        except Exception:
            _LOGGER.exception("Failed to query fan sensors")
            fan_sensors = []

        if not fan_sensors:
            # No fans found — skip
            self._options[CONF_FANS] = []
            return self._create_entry()

        fan_options = [
            SelectOptionDict(value=name, label=name) for name in fan_sensors
        ]

        return self.async_show_form(
            step_id="fan_select",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SELECTED_FANS): SelectSelector(
                        SelectSelectorConfig(
                            options=fan_options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_fan_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: Configure thresholds for each selected fan (loops)."""
        if user_input is not None:
            fan_name = self._selected_fans[self._fan_index]
            fan_entry: dict[str, Any] = {"name": fan_name, "thresholds": {}}

            lower = [
                user_input.get(CONF_FAN_LNR),
                user_input.get(CONF_FAN_LC),
                user_input.get(CONF_FAN_LNC),
            ]
            upper = [
                user_input.get(CONF_FAN_UNC),
                user_input.get(CONF_FAN_UC),
                user_input.get(CONF_FAN_UNR),
            ]

            if any(v is not None for v in lower):
                fan_entry["thresholds"]["lower"] = [v or 0 for v in lower]
            if any(v is not None for v in upper):
                fan_entry["thresholds"]["upper"] = [v or 0 for v in upper]

            self._options.setdefault(CONF_FANS, []).append(fan_entry)
            self._fan_index += 1

            if self._fan_index < len(self._selected_fans):
                return await self.async_step_fan_thresholds()

            return self._create_entry()

        # Read current thresholds from BMC for pre-fill
        fan_name = self._selected_fans[self._fan_index]
        defaults = await self._read_fan_thresholds(fan_name)

        return self.async_show_form(
            step_id="fan_thresholds",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FAN_LNR, default=defaults.get("lnr")): int,
                    vol.Optional(CONF_FAN_LC, default=defaults.get("lc")): int,
                    vol.Optional(CONF_FAN_LNC, default=defaults.get("lnc")): int,
                    vol.Optional(CONF_FAN_UNC, default=defaults.get("unc")): int,
                    vol.Optional(CONF_FAN_UC, default=defaults.get("uc")): int,
                    vol.Optional(CONF_FAN_UNR, default=defaults.get("unr")): int,
                }
            ),
            description_placeholders={"fan_name": fan_name},
        )

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"IPMI {self._data[CONF_HOST_NAME].title()}",
            data=self._data,
            options=self._options,
        )

    async def _read_fan_thresholds(self, fan_name: str) -> dict[str, int]:
        """Read current thresholds for a fan from the BMC."""
        client = self._get_client()
        try:
            sensor_num = await self.hass.async_add_executor_job(
                client._get_sensor_number, fan_name
            )
            if sensor_num is not None:
                thresholds = await self.hass.async_add_executor_job(
                    client.get_fan_thresholds, sensor_num
                )
                if thresholds:
                    return thresholds
        except Exception:
            _LOGGER.debug("Could not read thresholds for %s, using empty defaults", fan_name)
        return {}

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            error = await self._test_connection(
                reauth_entry.data[CONF_IPMI_IP],
                user_input[CONF_OPERATOR_USER],
                user_input[CONF_OPERATOR_PASS],
            )
            if error:
                errors["base"] = error
            else:
                updated_data = {**reauth_entry.data, **user_input}
                return self.async_update_reload_and_abort(
                    reauth_entry, data=updated_data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OPERATOR_USER,
                        default=reauth_entry.data[CONF_OPERATOR_USER],
                    ): str,
                    vol.Required(CONF_OPERATOR_PASS): str,
                    vol.Required(
                        CONF_ADMIN_USER,
                        default=reauth_entry.data[CONF_ADMIN_USER],
                    ): str,
                    vol.Required(CONF_ADMIN_PASS): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of connection details."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            error = await self._test_connection(
                user_input[CONF_IPMI_IP],
                user_input[CONF_OPERATOR_USER],
                user_input[CONF_OPERATOR_PASS],
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry, data=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST_NAME,
                        default=reconfigure_entry.data[CONF_HOST_NAME],
                    ): str,
                    vol.Required(
                        CONF_IPMI_IP,
                        default=reconfigure_entry.data[CONF_IPMI_IP],
                    ): str,
                    vol.Required(
                        CONF_OPERATOR_USER,
                        default=reconfigure_entry.data[CONF_OPERATOR_USER],
                    ): str,
                    vol.Required(CONF_OPERATOR_PASS): str,
                    vol.Required(
                        CONF_ADMIN_USER,
                        default=reconfigure_entry.data[CONF_ADMIN_USER],
                    ): str,
                    vol.Required(CONF_ADMIN_PASS): str,
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

    async def _test_connection(
        self, ip: str, user: str, password: str
    ) -> str | None:
        """Test IPMI connection. Returns error key or None on success."""
        try:
            await self.hass.async_add_executor_job(
                IpmiClient.test_connection, ip, user, password
            )
        except IpmiAuthError:
            return "invalid_auth"
        except IpmiConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during IPMI connection test")
            return "unknown"
        return None


class IpmiControllerOptionsFlow(OptionsFlow):
    """Handle options flow for IPMI Controller."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._new_options: dict[str, Any] = {}
        self._client: IpmiClient | None = None
        self._selected_fans: list[str] = []
        self._fan_index: int = 0

    def _get_client(self) -> IpmiClient:
        """Get or create an IpmiClient from config entry data."""
        if self._client is None:
            data = self._config_entry.data
            self._client = IpmiClient(
                ip=data[CONF_IPMI_IP],
                operator_user=data[CONF_OPERATOR_USER],
                operator_pass=data[CONF_OPERATOR_PASS],
                admin_user=data[CONF_ADMIN_USER],
                admin_pass=data[CONF_ADMIN_PASS],
            )
        return self._client

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow — power, scan, motherboard settings."""
        if user_input is not None:
            motherboard = user_input.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE)
            self._new_options = {**self._config_entry.options, **user_input}

            if motherboard != MOTHERBOARD_NONE and motherboard in MOTHERBOARD_PROFILES:
                profile = MOTHERBOARD_PROFILES[motherboard]
                self._new_options[CONF_FAN_MODES] = profile["fan_modes"]
                self._new_options[CONF_FAN_MODE_DISPLAY_MAPPING] = profile[
                    "fan_mode_display_mapping"
                ]
                self._new_options[CONF_FAN_MODE_QUERY_COMMAND] = profile[
                    "fan_mode_query_command"
                ]
                self._new_options[CONF_FAN_MODE_RESPONSE_MAPPING] = profile[
                    "fan_mode_response_mapping"
                ]
                self._new_options[CONF_FAN_MODE_COMMANDS] = profile["fan_mode_commands"]

            return await self.async_step_fan_select()

        current_opts = self._config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_CONTROL,
                        default=current_opts.get(
                            CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL
                        ),
                    ): vol.In(POWER_CONTROL_OPTIONS),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_opts.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_MOTHERBOARD,
                        default=current_opts.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE),
                    ): vol.In(MOTHERBOARD_OPTIONS),
                }
            ),
        )

    async def async_step_fan_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select fans for threshold configuration."""
        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_FANS, [])
            if selected:
                self._selected_fans = selected
                self._fan_index = 0
                return await self.async_step_fan_thresholds()
            else:
                self._new_options[CONF_FANS] = []
                return self.async_create_entry(title="", data=self._new_options)

        # Query SDR for fan sensors
        client = self._get_client()
        try:
            fan_sensors = await self.hass.async_add_executor_job(
                client.get_fan_sensors
            )
        except Exception:
            _LOGGER.exception("Failed to query fan sensors")
            fan_sensors = []

        if not fan_sensors:
            self._new_options[CONF_FANS] = []
            return self.async_create_entry(title="", data=self._new_options)

        # Pre-select currently configured fans
        current_fan_names = [
            f["name"] for f in self._config_entry.options.get(CONF_FANS, [])
        ]
        default_selection = [n for n in current_fan_names if n in fan_sensors]

        fan_options = [
            SelectOptionDict(value=name, label=name) for name in fan_sensors
        ]

        return self.async_show_form(
            step_id="fan_select",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_FANS, default=default_selection
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=fan_options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_fan_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure thresholds for each selected fan (loops)."""
        if user_input is not None:
            fan_name = self._selected_fans[self._fan_index]
            fan_entry: dict[str, Any] = {"name": fan_name, "thresholds": {}}

            lower = [
                user_input.get(CONF_FAN_LNR),
                user_input.get(CONF_FAN_LC),
                user_input.get(CONF_FAN_LNC),
            ]
            upper = [
                user_input.get(CONF_FAN_UNC),
                user_input.get(CONF_FAN_UC),
                user_input.get(CONF_FAN_UNR),
            ]

            if any(v is not None for v in lower):
                fan_entry["thresholds"]["lower"] = [v or 0 for v in lower]
            if any(v is not None for v in upper):
                fan_entry["thresholds"]["upper"] = [v or 0 for v in upper]

            self._new_options.setdefault(CONF_FANS, []).append(fan_entry)
            self._fan_index += 1

            if self._fan_index < len(self._selected_fans):
                return await self.async_step_fan_thresholds()

            return self.async_create_entry(title="", data=self._new_options)

        # Read current thresholds for pre-fill
        fan_name = self._selected_fans[self._fan_index]
        defaults = await self._read_fan_thresholds(fan_name)

        return self.async_show_form(
            step_id="fan_thresholds",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FAN_LNR, default=defaults.get("lnr")): int,
                    vol.Optional(CONF_FAN_LC, default=defaults.get("lc")): int,
                    vol.Optional(CONF_FAN_LNC, default=defaults.get("lnc")): int,
                    vol.Optional(CONF_FAN_UNC, default=defaults.get("unc")): int,
                    vol.Optional(CONF_FAN_UC, default=defaults.get("uc")): int,
                    vol.Optional(CONF_FAN_UNR, default=defaults.get("unr")): int,
                }
            ),
            description_placeholders={"fan_name": fan_name},
        )

    async def _read_fan_thresholds(self, fan_name: str) -> dict[str, int]:
        """Read current thresholds for a fan from the BMC."""
        # First check if we have configured values
        for fan in self._config_entry.options.get(CONF_FANS, []):
            if fan["name"] == fan_name:
                thresholds = fan.get("thresholds", {})
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
            sensor_num = await self.hass.async_add_executor_job(
                client._get_sensor_number, fan_name
            )
            if sensor_num is not None:
                thresholds = await self.hass.async_add_executor_job(
                    client.get_fan_thresholds, sensor_num
                )
                if thresholds:
                    return thresholds
        except Exception:
            _LOGGER.debug(
                "Could not read thresholds for %s, using empty defaults", fan_name
            )
        return {}
