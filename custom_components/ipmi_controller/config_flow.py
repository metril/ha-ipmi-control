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

from .const import (
    CONF_ADMIN_PASS,
    CONF_ADMIN_USER,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_DISPLAY_MAPPING,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_FAN_MODES,
    CONF_FANS,
    CONF_HOST_NAME,
    CONF_IPMI_IP,
    CONF_MOTHERBOARD,
    CONF_OPERATOR_PASS,
    CONF_OPERATOR_USER,
    CONF_POWER_CONTROL,
    CONF_SCAN_INTERVAL,
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Connection credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Test connection with operator credentials
            error = await self._test_connection(
                user_input[CONF_IPMI_IP],
                user_input[CONF_OPERATOR_USER],
                user_input[CONF_OPERATOR_PASS],
            )
            if error:
                errors["base"] = error
            else:
                # Use host name as unique ID
                await self.async_set_unique_id(user_input[CONF_HOST_NAME])
                self._abort_if_unique_id_configured()

                self._data = user_input
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

            return await self.async_step_fans()

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

    async def async_step_fans(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Configure fan sensors and thresholds."""
        if user_input is not None:
            fans_raw = user_input.get(CONF_FANS, "")
            fans = self._parse_fans_input(fans_raw)
            self._options[CONF_FANS] = fans

            return self.async_create_entry(
                title=f"IPMI {self._data[CONF_HOST_NAME].title()}",
                data=self._data,
                options=self._options,
            )

        return self.async_show_form(
            step_id="fans",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FANS, default=""): str,
                }
            ),
            description_placeholders={
                "fans_format": "FAN1:75,150,225:3150,3300,3450;FAN2:100,200,300:2200,2300,2400"
            },
        )

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

    @staticmethod
    def _parse_fans_input(raw: str) -> list[dict]:
        """Parse fans input string.

        Format: FAN1:75,150,225:3150,3300,3450;FAN2:100,200,300:2200,2300,2400
        """
        if not raw.strip():
            return []

        fans = []
        for fan_str in raw.split(";"):
            fan_str = fan_str.strip()
            if not fan_str:
                continue

            parts = fan_str.split(":")
            fan_name = parts[0].strip()
            fan_entry: dict[str, Any] = {"name": fan_name}

            if len(parts) >= 2:
                lower = [int(x.strip()) for x in parts[1].split(",") if x.strip()]
                if lower:
                    fan_entry.setdefault("thresholds", {})["lower"] = lower

            if len(parts) >= 3:
                upper = [int(x.strip()) for x in parts[2].split(",") if x.strip()]
                if upper:
                    fan_entry.setdefault("thresholds", {})["upper"] = upper

            fans.append(fan_entry)

        return fans


class IpmiControllerOptionsFlow(OptionsFlow):
    """Handle options flow for IPMI Controller."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            # Parse fans string
            fans_raw = user_input.pop(CONF_FANS, "")
            fans = IpmiControllerConfigFlow._parse_fans_input(fans_raw)

            # Merge motherboard profile if changed
            motherboard = user_input.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE)
            new_options = {**self._config_entry.options, **user_input}
            new_options[CONF_FANS] = fans

            if motherboard != MOTHERBOARD_NONE and motherboard in MOTHERBOARD_PROFILES:
                profile = MOTHERBOARD_PROFILES[motherboard]
                new_options[CONF_FAN_MODES] = profile["fan_modes"]
                new_options[CONF_FAN_MODE_DISPLAY_MAPPING] = profile[
                    "fan_mode_display_mapping"
                ]
                new_options[CONF_FAN_MODE_QUERY_COMMAND] = profile[
                    "fan_mode_query_command"
                ]
                new_options[CONF_FAN_MODE_RESPONSE_MAPPING] = profile[
                    "fan_mode_response_mapping"
                ]
                new_options[CONF_FAN_MODE_COMMANDS] = profile["fan_mode_commands"]

            return self.async_create_entry(title="", data=new_options)

        current_opts = self._config_entry.options
        current_fans = current_opts.get(CONF_FANS, [])
        fans_str = self._format_fans_for_display(current_fans)

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
                    vol.Optional(CONF_FANS, default=fans_str): str,
                }
            ),
        )

    @staticmethod
    def _format_fans_for_display(fans: list[dict]) -> str:
        """Format fans list back to input string."""
        parts = []
        for fan in fans:
            name = fan["name"]
            thresholds = fan.get("thresholds", {})
            lower = ",".join(str(v) for v in thresholds.get("lower", []))
            upper = ",".join(str(v) for v in thresholds.get("upper", []))
            if lower or upper:
                parts.append(f"{name}:{lower}:{upper}")
            else:
                parts.append(name)
        return ";".join(parts)
