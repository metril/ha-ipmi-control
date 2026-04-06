"""DataUpdateCoordinator for IPMI Controller."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_HOST_NAME, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)


class IpmiDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to poll IPMI state for a single host."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.entry = entry
        host_name = entry.data[CONF_HOST_NAME]
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=f"IPMI {host_name}",
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from IPMI."""
        try:
            power_state = await self.hass.async_add_executor_job(
                self.client.get_chassis_status
            )
        except IpmiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IpmiConnectionError as err:
            raise UpdateFailed(str(err)) from err

        fan_mode = None
        if self.client._fan_config.get("fan_mode_query_command"):
            try:
                fan_mode = await self.hass.async_add_executor_job(
                    self.client.get_fan_mode
                )
            except IpmiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IpmiConnectionError as err:
                raise UpdateFailed(str(err)) from err

        return {
            "power": power_state,
            "fan_mode": fan_mode,
        }
