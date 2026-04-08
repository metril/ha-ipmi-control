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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADDON_URL,
    CONF_FAN_MODE_COMMANDS,
    CONF_FAN_MODE_DISPLAY_MAPPING,
    CONF_FAN_MODE_QUERY_COMMAND,
    CONF_FAN_MODE_RESPONSE_MAPPING,
    CONF_FAN_MODES,
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

CONF_MANUAL_SENSORS = "manual_sensors"
DEFAULT_ADDON_URL = "http://local-ipmi-control:8099"

PRIVILEGE_LEVELS = ["ADMINISTRATOR", "OPERATOR"]

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

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._addon_url: str = DEFAULT_ADDON_URL
        self._client: IpmiClient | None = None
        self._sdr_units: dict[str, str] = {}

    def _get_client(self) -> IpmiClient:
        """Get or create an IpmiClient from collected data."""
        if self._client is None:
            session = async_get_clientsession(self.hass)
            self._client = IpmiClient(
                session=session,
                addon_url=self._addon_url,
                host_ip=self._data[CONF_IPMI_IP],
                username=self._data[CONF_USERNAME],
                password=self._data[CONF_PASSWORD],
                privilege_level=self._data[CONF_PRIVILEGE_LEVEL],
            )
        return self._client

    async def _detect_addon_url(self) -> bool:
        """Auto-detect the add-on URL via Supervisor. Returns True if found."""
        if self._addon_url != DEFAULT_ADDON_URL:
            return True  # Already detected (e.g., via hassio discovery)
        # Try the default URL — it may work for local add-ons
        session = async_get_clientsession(self.hass)
        try:
            await IpmiClient.test_addon_connection(session, self._addon_url)
            return True
        except IpmiConnectionError:
            return False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: IPMI connection credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)

            # Verify add-on is reachable
            if not await self._detect_addon_url():
                errors["base"] = "addon_not_reachable"
            else:
                # Verify IPMI connection
                try:
                    await IpmiClient.test_ipmi_connection(
                        session,
                        self._addon_url,
                        user_input[CONF_IPMI_IP],
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                    )
                except IpmiAuthError:
                    errors["base"] = "invalid_auth"
                except IpmiConnectionError:
                    errors["base"] = "cannot_connect"

            # Verify admin privilege if selected
            if not errors and user_input[CONF_PRIVILEGE_LEVEL] == "ADMINISTRATOR":
                try:
                    await IpmiClient.test_admin_privilege(
                        session,
                        self._addon_url,
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

                user_input[CONF_ADDON_URL] = self._addon_url
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
                }
            ),
            errors=errors,
        )

    async def async_step_hassio(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle Supervisor add-on discovery."""
        self._addon_url = f"http://{discovery_info.get('host', 'local-ipmi-control')}:{discovery_info.get('port', 8099)}"
        return await self.async_step_user()

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
                SelectOptionDict(value=s["name"], label=f"{s['name']} ({s['unit']})" if s["unit"] else s["name"])
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
            addon_url = reconfigure_entry.data[CONF_ADDON_URL]
            session = async_get_clientsession(self.hass)
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
                # Preserve host name and addon URL from original entry
                user_input[CONF_HOST_NAME] = reconfigure_entry.data[CONF_HOST_NAME]
                user_input[CONF_ADDON_URL] = addon_url
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

            return await self.async_step_sensor_select()

        current_opts = self._config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POWER_CONTROL,
                        default=current_opts.get(CONF_POWER_CONTROL, DEFAULT_POWER_CONTROL),
                    ): vol.In(POWER_CONTROL_OPTIONS),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_MOTHERBOARD,
                        default=current_opts.get(CONF_MOTHERBOARD, MOTHERBOARD_NONE),
                    ): vol.In(MOTHERBOARD_OPTIONS),
                }
            ),
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
                SelectOptionDict(value=s["name"], label=f"{s['name']} ({s['unit']})" if s["unit"] else s["name"])
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
