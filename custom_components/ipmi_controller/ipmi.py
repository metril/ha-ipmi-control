"""IPMI client wrapper using pyghmi for pure-Python IPMI 2.0 communication."""

from __future__ import annotations

import logging
import struct
from typing import Any

from pyghmi.ipmi import command as ipmi_command
from pyghmi.exceptions import IpmiException

_LOGGER = logging.getLogger(__name__)

# IPMI Set Sensor Thresholds command
SENSOR_EVENT_NETFN = 0x04
SET_SENSOR_THRESHOLD_CMD = 0x26


class IpmiConnectionError(Exception):
    """Raised when IPMI connection fails."""


class IpmiAuthError(Exception):
    """Raised when IPMI authentication fails."""


class IpmiClient:
    """Wrapper around pyghmi for IPMI operations."""

    def __init__(
        self,
        ip: str,
        operator_user: str,
        operator_pass: str,
        admin_user: str,
        admin_pass: str,
        fan_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize IPMI client."""
        self.ip = ip
        self._operator_creds = (operator_user, operator_pass)
        self._admin_creds = (admin_user, admin_pass)
        self._fan_config = fan_config or {}
        self._sdr_cache: dict[str, int] | None = None

    def _get_connection(self, privilege: str = "operator") -> ipmi_command.Command:
        """Create an IPMI connection with appropriate credentials.

        Connections are not reused because pyghmi sessions can go stale
        between polling intervals. Creating a fresh connection per operation
        is the safest approach for a polling-based integration.
        """
        if privilege == "administrator":
            user, passwd = self._admin_creds
        else:
            user, passwd = self._operator_creds

        try:
            return ipmi_command.Command(
                bmc=self.ip,
                userid=user,
                password=passwd,
            )
        except IpmiException as err:
            error_msg = str(err).lower()
            if "unauthorized" in error_msg or "password" in error_msg:
                raise IpmiAuthError(
                    f"Authentication failed for {self.ip}"
                ) from err
            raise IpmiConnectionError(
                f"Failed to connect to {self.ip}: {err}"
            ) from err
        except Exception as err:
            raise IpmiConnectionError(
                f"Failed to connect to {self.ip}: {err}"
            ) from err

    def get_chassis_status(self) -> bool | None:
        """Get chassis power status. Returns True=on, False=off, None=error."""
        try:
            conn = self._get_connection("operator")
            try:
                result = conn.get_power()
                power_state = result.get("powerstate", "")
                return power_state == "on"
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error getting chassis status from %s: %s", self.ip, err)
            return None

    def power_on(self) -> bool:
        """Send power on command."""
        try:
            conn = self._get_connection("operator")
            try:
                conn.set_power("on")
                return True
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error powering on %s: %s", self.ip, err)
            return False

    def power_off(self) -> bool:
        """Send soft power off (ACPI shutdown) command."""
        try:
            conn = self._get_connection("operator")
            try:
                conn.set_power("shutdown")
                return True
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error powering off %s: %s", self.ip, err)
            return False

    def get_fan_mode(self) -> str | None:
        """Query current fan mode using configured raw command."""
        query_cmd = self._fan_config.get("fan_mode_query_command")
        if not query_cmd:
            return None

        response_mapping = self._fan_config.get("fan_mode_response_mapping", {})

        try:
            conn = self._get_connection("administrator")
            try:
                result = conn.xraw_command(
                    netfn=query_cmd["netfn"],
                    command=query_cmd["command"],
                    data=bytes(query_cmd.get("data", [])),
                )
                if result and "data" in result:
                    response_byte = result["data"][0]
                    return response_mapping.get(response_byte)
                return None
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error querying fan mode from %s: %s", self.ip, err)
            return None

    def set_fan_mode(self, mode: str) -> bool:
        """Set fan mode by executing configured raw commands."""
        mode_commands = self._fan_config.get("fan_mode_commands", {})
        commands = mode_commands.get(mode)
        if not commands:
            _LOGGER.error("No commands configured for fan mode '%s'", mode)
            return False

        try:
            conn = self._get_connection("administrator")
            try:
                for cmd in commands:
                    conn.xraw_command(
                        netfn=cmd["netfn"],
                        command=cmd["command"],
                        data=bytes(cmd.get("data", [])),
                    )
                return True
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error setting fan mode on %s: %s", self.ip, err)
            return False

    def _build_sdr_cache(self) -> dict[str, int]:
        """Query SDR to build fan_name → sensor_number mapping."""
        cache: dict[str, int] = {}
        try:
            conn = self._get_connection("operator")
            try:
                sdr_data = conn.get_sensor_data()
                for sensor_name, sensor_info in sdr_data.items():
                    cache[sensor_name] = sensor_info.get("sensor_number", -1)
            finally:
                conn.ipmi_session.logout()
        except Exception as err:
            _LOGGER.error("Error building SDR cache from %s: %s", self.ip, err)
        return cache

    def _get_sensor_number(self, fan_name: str) -> int | None:
        """Get sensor number for a fan name, building SDR cache if needed."""
        if self._sdr_cache is None:
            self._sdr_cache = self._build_sdr_cache()

        sensor_num = self._sdr_cache.get(fan_name)
        if sensor_num is None:
            _LOGGER.error(
                "Fan '%s' not found in SDR on %s. Available sensors: %s",
                fan_name,
                self.ip,
                list(self._sdr_cache.keys()),
            )
        return sensor_num

    def set_fan_thresholds(self, fans: list[dict]) -> bool:
        """Set fan sensor thresholds for all configured fans.

        Each fan dict has: name, thresholds: {lower: [LNR, LC, LNC], upper: [UNC, UC, UNR]}
        """
        if not fans:
            return True

        success = True
        try:
            conn = self._get_connection("administrator")
            try:
                for fan in fans:
                    fan_name = fan["name"]
                    thresholds = fan.get("thresholds", {})
                    if not thresholds:
                        continue

                    sensor_num = self._get_sensor_number(fan_name)
                    if sensor_num is None:
                        success = False
                        continue

                    lower = thresholds.get("lower", [])
                    upper = thresholds.get("upper", [])

                    if lower and len(lower) >= 3:
                        # Set lower thresholds: mask 0x07 = bits 0,1,2 (LNR, LC, LNC)
                        data = bytes([
                            sensor_num,
                            0x07,  # set LNR, LC, LNC
                            lower[0] & 0xFF,  # LNR
                            lower[1] & 0xFF,  # LC
                            lower[2] & 0xFF,  # LNC
                            0, 0, 0,  # UNC, UC, UNR unused
                        ])
                        try:
                            conn.xraw_command(
                                netfn=SENSOR_EVENT_NETFN,
                                command=SET_SENSOR_THRESHOLD_CMD,
                                data=data,
                            )
                            _LOGGER.debug(
                                "Set lower thresholds for %s: %s", fan_name, lower
                            )
                        except Exception as err:
                            _LOGGER.error(
                                "Failed to set lower thresholds for %s: %s",
                                fan_name, err,
                            )
                            success = False

                    if upper and len(upper) >= 3:
                        # Set upper thresholds: mask 0x38 = bits 3,4,5 (UNC, UC, UNR)
                        data = bytes([
                            sensor_num,
                            0x38,  # set UNC, UC, UNR
                            0, 0, 0,  # LNR, LC, LNC unused
                            upper[0] & 0xFF,  # UNC
                            upper[1] & 0xFF,  # UC
                            upper[2] & 0xFF,  # UNR
                        ])
                        try:
                            conn.xraw_command(
                                netfn=SENSOR_EVENT_NETFN,
                                command=SET_SENSOR_THRESHOLD_CMD,
                                data=data,
                            )
                            _LOGGER.debug(
                                "Set upper thresholds for %s: %s", fan_name, upper
                            )
                        except Exception as err:
                            _LOGGER.error(
                                "Failed to set upper thresholds for %s: %s",
                                fan_name, err,
                            )
                            success = False
            finally:
                conn.ipmi_session.logout()
        except (IpmiAuthError, IpmiConnectionError):
            raise
        except Exception as err:
            _LOGGER.error("Error setting fan thresholds on %s: %s", self.ip, err)
            return False

        return success

    @staticmethod
    def test_connection(ip: str, user: str, password: str) -> None:
        """Test IPMI connection. Raises IpmiConnectionError or IpmiAuthError on failure."""
        try:
            conn = ipmi_command.Command(bmc=ip, userid=user, password=password)
            try:
                conn.get_power()
            finally:
                conn.ipmi_session.logout()
        except IpmiException as err:
            error_msg = str(err).lower()
            if "unauthorized" in error_msg or "password" in error_msg:
                raise IpmiAuthError(f"Authentication failed for {ip}") from err
            raise IpmiConnectionError(f"Failed to connect to {ip}: {err}") from err
        except Exception as err:
            raise IpmiConnectionError(f"Failed to connect to {ip}: {err}") from err
