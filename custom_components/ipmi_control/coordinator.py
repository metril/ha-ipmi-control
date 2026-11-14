"""DataUpdateCoordinator for IPMI Controller."""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_HOST_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SENSOR_SCAN_INTERVAL,
    CONF_SENSORS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SENSOR_SCAN_INTERVAL,
)
from .data import IpmiConfigEntry
from .ipmi import IpmiAuthError, IpmiClient, IpmiConnectionError

_LOGGER = logging.getLogger(__name__)

THRESHOLD_RETRY_INTERVAL = 300  # seconds between threshold fetch retries


class IpmiDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to poll IPMI state for a single host."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IpmiConfigEntry,
        client: IpmiClient,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self._thresholds_loaded = False
        self._thresholds_next_try = 0.0
        self._thresholds_warned = False
        self._sensors_next_poll = 0.0
        host_name = entry.data[CONF_HOST_NAME]
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        sensor_interval = entry.options.get(
            CONF_SENSOR_SCAN_INTERVAL, DEFAULT_SENSOR_SCAN_INTERVAL
        )
        # SDR readings can never be polled faster than the main poll.
        self._sensor_interval = max(sensor_interval, scan_interval)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"IPMI {host_name}",
            update_interval=timedelta(seconds=scan_interval),
        )

    def _in_bmc_reset_grace(self) -> bool:
        """Return True while a recent BMC cold reset is still expected to bite."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        grace_until = getattr(runtime, "bmc_reset_grace_until", None)
        return grace_until is not None and time.monotonic() < grace_until

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from IPMI, tolerating a BMC that is mid-reset.

        Polling continues at the normal interval throughout the grace window;
        only the classification of connection failures changes, so entities hold
        their last known state instead of flapping to unavailable while the BMC
        reboots. The window ends on its own once a poll succeeds.
        """
        try:
            return await self._fetch_data()
        except UpdateFailed as err:
            if not self._in_bmc_reset_grace():
                raise
            # ConfigEntryAuthFailed is not an UpdateFailed and is deliberately
            # not caught here: a rebooting BMC does not produce auth errors, and
            # swallowing them would break reauth.
            _LOGGER.debug(
                "Ignoring IPMI poll failure during BMC reset grace window: %s", err
            )
            if self.data is not None:
                return self.data
            return {
                "power": None,
                "fan_mode": None,
                "sensor_thresholds": {},
                "sensor_readings": {},
            }

    async def _fetch_data(self) -> dict[str, Any]:
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

        prev = self.data
        sensor_readings: dict[str, dict] = (
            dict(prev.get("sensor_readings", {})) if prev is not None else {}
        )
        sensors = self.config_entry.options.get(CONF_SENSORS, [])
        # SDR readings are the expensive call: fetch on first poll, when the
        # slow interval elapsed, or right after a power state change.
        sensors_due = (
            prev is None
            or time.monotonic() >= self._sensors_next_poll
            or prev.get("power") != power_state
        )
        if sensors and sensors_due:
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

            self._sensors_next_poll = time.monotonic() + self._sensor_interval
            self._persist_learned_units(sensors, sensor_readings)

        # Fetch thresholds until one fetch returns data; afterwards refreshes are
        # on-demand. `ipmitool sensor get` fails (empty stderr) when a sensor's
        # reading is unavailable, which is normal while the host is powered off,
        # so skip while off and back off between attempts.
        sensor_thresholds = (
            self.data.get("sensor_thresholds", {}) if self.data is not None else {}
        )
        now = time.monotonic()
        if (
            not self._thresholds_loaded
            and sensors
            and power_state
            and now >= self._thresholds_next_try
        ):
            try:
                fetched = await self.client.get_all_sensor_thresholds(sensors)
            except IpmiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IpmiConnectionError as err:
                self._thresholds_next_try = now + THRESHOLD_RETRY_INTERVAL
                log = _LOGGER.debug if self._thresholds_warned else _LOGGER.warning
                self._thresholds_warned = True
                log(
                    "Failed to fetch thresholds (retrying in %ss): %s",
                    THRESHOLD_RETRY_INTERVAL,
                    err or "sensor readings unavailable",
                )
            else:
                if fetched:
                    sensor_thresholds = fetched
                    self._thresholds_loaded = True
                else:
                    # No sensor reported thresholds; check again later.
                    self._thresholds_next_try = now + THRESHOLD_RETRY_INTERVAL

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
        # Re-read at write time: the options may have changed since the caller
        # captured `sensors`, and writing that stale list back would clobber them.
        current: list[dict] = self.config_entry.options.get(CONF_SENSORS, [])
        updated: list[dict] = []
        changed = False
        for sensor in current:
            live_unit = readings.get(sensor["name"], {}).get("unit")
            if live_unit and not sensor.get("unit"):
                updated.append({**sensor, "unit": live_unit})
                changed = True
            else:
                updated.append(sensor)

        if not changed:
            return

        _LOGGER.debug(
            "Updating stored SDR units for %s", self.config_entry.data[CONF_HOST_NAME]
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={**self.config_entry.options, CONF_SENSORS: updated},
        )

    async def async_refresh_thresholds(self) -> None:
        """Fetch sensor thresholds from BMC and update stored data.

        Raises:
            IpmiConnectionError: If the BMC cannot be reached or the request
                fails. Callers must not swallow this silently.
        """
        sensors = self.config_entry.options.get(CONF_SENSORS, [])
        if not sensors or self.data is None:
            return
        try:
            sensor_thresholds = await self.client.get_all_sensor_thresholds(sensors)
            merged = {**self.data.get("sensor_thresholds", {}), **sensor_thresholds}
            self._thresholds_loaded = True
            self.async_set_updated_data({**self.data, "sensor_thresholds": merged})
        except IpmiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IpmiConnectionError as err:
            _LOGGER.error("Failed to refresh thresholds: %s", err)
            raise
