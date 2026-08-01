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

from .const import CONF_HOST_NAME, CONF_SCAN_INTERVAL, CONF_SENSORS, DEFAULT_SCAN_INTERVAL
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
        """Fetch data from IPMI via add-on."""
        try:
            power_state = await self.client.get_chassis_status()
        except IpmiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IpmiConnectionError as err:
            raise UpdateFailed(str(err)) from err

        fan_mode = None
        if self.client.has_fan_mode_query and self.client.is_admin:
            try:
                fan_mode = await self.client.get_fan_mode()
            except IpmiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IpmiConnectionError as err:
                raise UpdateFailed(str(err)) from err

        sensor_readings: dict[str, dict] = {}
        sensors = self.entry.options.get(CONF_SENSORS, [])
        if sensors:
            try:
                all_readings = await self.client.get_sdr_readings()
                selected_names = {s["name"] for s in sensors}
                sensor_readings = {
                    k: v for k, v in all_readings.items() if k in selected_names
                }
            except IpmiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IpmiConnectionError as err:
                raise UpdateFailed(str(err)) from err

            self._persist_learned_units(sensors, sensor_readings)

        # Fetch thresholds on first run only; subsequent refreshes are on-demand
        sensor_thresholds = self.data.get("sensor_thresholds", {}) if self.data else {}
        if not self.data and sensors:
            try:
                sensor_thresholds = await self.client.get_all_sensor_thresholds(sensors)
            except IpmiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IpmiConnectionError as err:
                _LOGGER.warning("Failed to fetch initial thresholds: %s", err)

        return {
            "power": power_state,
            "fan_mode": fan_mode,
            "sensor_thresholds": sensor_thresholds,
            "sensor_readings": sensor_readings,
        }

    def _persist_learned_units(
        self, sensors: list[dict], readings: dict[str, dict]
    ) -> None:
        """Write units learned from live readings back into the config entry.

        Units are captured during the config flow, but a sensor that was unreadable
        at that moment (an idle fan reporting "ns", or one the add-on failed to parse)
        gets stored with an empty unit and would otherwise keep it forever. Correcting
        the stored value means the unit is right at entity-init on the next restart,
        and the options flow shows accurate labels.

        Only ever fills in or corrects a unit from a live non-empty reading; never
        clears one, so a fan dropping to "ns" does not lose its RPM unit.
        """
        updated: list[dict] = []
        changed = False
        for sensor in sensors:
            live_unit = readings.get(sensor["name"], {}).get("unit")
            if live_unit and live_unit != sensor.get("unit"):
                updated.append({**sensor, "unit": live_unit})
                changed = True
            else:
                updated.append(sensor)

        if not changed:
            return

        _LOGGER.debug(
            "Updating stored SDR units for %s", self.entry.data[CONF_HOST_NAME]
        )
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_SENSORS: updated},
        )

    async def async_refresh_thresholds(self) -> None:
        """Fetch sensor thresholds from BMC and update stored data.

        Raises:
            IpmiConnectionError: If the BMC cannot be reached or the request
                fails. Callers must not swallow this silently.
        """
        sensors = self.entry.options.get(CONF_SENSORS, [])
        if not sensors or self.data is None:
            return
        try:
            sensor_thresholds = await self.client.get_all_sensor_thresholds(sensors)
            self.async_set_updated_data(
                {**self.data, "sensor_thresholds": sensor_thresholds}
            )
        except IpmiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IpmiConnectionError as err:
            _LOGGER.error("Failed to refresh thresholds: %s", err)
            raise
