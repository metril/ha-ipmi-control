"""Async HTTP client for the IPMI Control add-on API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class IpmiConnectionError(Exception):
    """Raised when the IPMI add-on connection fails."""


class IpmiAuthError(Exception):
    """Raised when IPMI authentication fails."""


class IpmiClient:
    """Client that communicates with the IPMI Control add-on via HTTP."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        addon_url: str,
        host_ip: str,
        operator_user: str,
        operator_pass: str,
        admin_user: str,
        admin_pass: str,
        fan_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the IPMI client."""
        self._session = session
        self._addon_url = addon_url.rstrip("/")
        self._host_ip = host_ip
        self._operator_creds = (operator_user, operator_pass)
        self._admin_creds = (admin_user, admin_pass)
        self._fan_config = fan_config or {}

    def _operator_body(self) -> dict[str, str]:
        """Build request body with operator credentials."""
        return {
            "host": self._host_ip,
            "user": self._operator_creds[0],
            "password": self._operator_creds[1],
        }

    def _admin_body(self) -> dict[str, str]:
        """Build request body with administrator credentials."""
        return {
            "host": self._host_ip,
            "user": self._admin_creds[0],
            "password": self._admin_creds[1],
        }

    async def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict[str, Any]:
        """Make a request to the add-on API."""
        url = f"{self._addon_url}{path}"
        try:
            async with self._session.request(
                method, url, json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    detail = (await resp.json()).get("detail", "Auth failed")
                    raise IpmiAuthError(detail)
                if resp.status != 200:
                    detail = (await resp.json()).get("detail", f"HTTP {resp.status}")
                    raise IpmiConnectionError(detail)
                return await resp.json()
        except aiohttp.ClientError as err:
            raise IpmiConnectionError(
                f"Failed to connect to IPMI add-on: {err}"
            ) from err

    @property
    def has_fan_mode_query(self) -> bool:
        """Return True if fan mode querying is configured."""
        return bool(self._fan_config.get("fan_mode_query_command"))

    async def check_addon_health(self) -> bool:
        """Check if the add-on is reachable."""
        result = await self._request("GET", "/api/health")
        return result.get("ok", False)

    async def get_chassis_status(self) -> bool:
        """Get chassis power status."""
        result = await self._request("POST", "/api/chassis/status", self._operator_body())
        return result["power"]

    async def power_on(self) -> None:
        """Send power on command."""
        body = {**self._operator_body(), "action": "on"}
        await self._request("POST", "/api/chassis/power", body)

    async def power_off(self) -> None:
        """Send soft power off command."""
        body = {**self._operator_body(), "action": "soft"}
        await self._request("POST", "/api/chassis/power", body)

    async def get_fan_mode(self) -> str | None:
        """Query current fan mode using configured raw command."""
        query_cmd = self._fan_config.get("fan_mode_query_command")
        if not query_cmd:
            return None

        response_mapping = self._fan_config.get("fan_mode_response_mapping", {})

        # Build the raw command string from the structured config
        netfn = query_cmd["netfn"]
        command = query_cmd["command"]
        data = query_cmd.get("data", [])
        raw_str = "raw " + " ".join(f"0x{b:02x}" for b in [netfn, command] + data)

        body = {
            **self._admin_body(),
            "privilege": "ADMINISTRATOR",
            "command": raw_str,
        }
        result = await self._request("POST", "/api/raw", body)
        output = result.get("output", "").strip()

        # Match response byte to mode name
        for response_key, mode_name in response_mapping.items():
            key = int(response_key) if isinstance(response_key, str) else response_key
            response_hex = f"{key:02x}"
            if response_hex in output.lower():
                return mode_name

        _LOGGER.warning("Unknown fan mode response: %s", output)
        return None

    async def set_fan_mode(self, mode: str) -> None:
        """Set fan mode by executing configured raw commands."""
        mode_commands = self._fan_config.get("fan_mode_commands", {})
        commands = mode_commands.get(mode)
        if not commands:
            raise IpmiConnectionError(f"No commands configured for fan mode '{mode}'")

        for cmd in commands:
            netfn = cmd["netfn"]
            command = cmd["command"]
            data = cmd.get("data", [])
            raw_str = "raw " + " ".join(f"0x{b:02x}" for b in [netfn, command] + data)

            body = {
                **self._admin_body(),
                "privilege": "ADMINISTRATOR",
                "command": raw_str,
            }
            await self._request("POST", "/api/raw", body)

    async def get_fan_sensors(self) -> list[str]:
        """Query SDR for fan sensor names."""
        result = await self._request("POST", "/api/sdr/fans", self._operator_body())
        return result.get("fans", [])

    async def get_fan_thresholds(self, sensor_name: str) -> dict[str, int] | None:
        """Read current threshold values for a sensor."""
        body = {**self._operator_body(), "sensor_name": sensor_name}
        result = await self._request("POST", "/api/sensor/thresholds/get", body)
        if result:
            return result
        return None

    async def get_all_fan_thresholds(
        self, fans: list[dict]
    ) -> dict[str, dict[str, int]]:
        """Read current thresholds for all configured fans."""
        result: dict[str, dict[str, int]] = {}
        for fan in fans:
            fan_name = fan["name"]
            thresholds = await self.get_fan_thresholds(fan_name)
            if thresholds:
                result[fan_name] = thresholds
        return result

    async def set_fan_thresholds(self, fans: list[dict]) -> bool:
        """Set fan sensor thresholds for all configured fans."""
        if not fans:
            return True

        success = True
        for fan in fans:
            fan_name = fan["name"]
            thresholds = fan.get("thresholds", {})
            if not thresholds:
                continue

            body: dict[str, Any] = {
                **self._admin_body(),
                "sensor_name": fan_name,
            }
            lower = thresholds.get("lower")
            upper = thresholds.get("upper")
            if lower:
                body["lower"] = lower
            if upper:
                body["upper"] = upper

            try:
                await self._request("POST", "/api/sensor/thresholds/set", body)
            except Exception as err:
                _LOGGER.error("Failed to set thresholds for %s: %s", fan_name, err)
                success = False

        return success

    @staticmethod
    async def test_addon_connection(
        session: aiohttp.ClientSession, addon_url: str
    ) -> None:
        """Test connection to the add-on. Raises on failure."""
        url = f"{addon_url.rstrip('/')}/api/health"
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    raise IpmiConnectionError(f"Add-on returned HTTP {resp.status}")
        except aiohttp.ClientError as err:
            raise IpmiConnectionError(f"Cannot reach IPMI add-on: {err}") from err

    @staticmethod
    async def test_ipmi_connection(
        session: aiohttp.ClientSession,
        addon_url: str,
        host_ip: str,
        user: str,
        password: str,
    ) -> None:
        """Test IPMI connection via the add-on. Raises on failure."""
        url = f"{addon_url.rstrip('/')}/api/chassis/status"
        body = {"host": host_ip, "user": user, "password": password}
        try:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 401:
                    raise IpmiAuthError("IPMI authentication failed")
                if resp.status != 200:
                    detail = (await resp.json()).get("detail", f"HTTP {resp.status}")
                    raise IpmiConnectionError(detail)
        except aiohttp.ClientError as err:
            raise IpmiConnectionError(f"Connection test failed: {err}") from err
