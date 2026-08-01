"""Sensor platform for IPMI Controller — general SDR sensor support."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST_NAME, CONF_SENSORS, DOMAIN
from .coordinator import IpmiDataUpdateCoordinator

THRESHOLD_ATTR_MAP = {
    "lnr": "lower_non_recoverable",
    "lc": "lower_critical",
    "lnc": "lower_non_critical",
    "unc": "upper_non_critical",
    "uc": "upper_critical",
    "unr": "upper_non_recoverable",
}

# ipmitool status for a sensor that reports nothing at all ("No Reading") \u2014 e.g. an
# unpopulated DIMM slot. Distinct from a threshold state like lnc, which is a real
# reading that happens to be out of range.
STATUS_NO_READING = "ns"

# Map normalized SDR unit strings to HA sensor properties. Keys must be lowercase with
# whitespace collapsed \u2014 look these up via _unit_config(), never directly.
SDR_UNIT_MAP: dict[str, dict] = {
    "degrees c": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit": UnitOfTemperature.CELSIUS,
        "icon": "mdi:thermometer",
        "precision": 0,
    },
    "degrees f": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit": UnitOfTemperature.FAHRENHEIT,
        "icon": "mdi:thermometer",
        "precision": 0,
    },
    "degrees k": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "native_unit": UnitOfTemperature.KELVIN,
        "icon": "mdi:thermometer",
        "precision": 0,
    },
    "volts": {
        "device_class": SensorDeviceClass.VOLTAGE,
        "native_unit": UnitOfElectricPotential.VOLT,
        "icon": "mdi:flash-triangle",
        "precision": 2,
    },
    "amps": {
        "device_class": SensorDeviceClass.CURRENT,
        "native_unit": UnitOfElectricCurrent.AMPERE,
        "icon": "mdi:flash",
        "precision": 2,
    },
    "watts": {
        "device_class": SensorDeviceClass.POWER,
        "native_unit": UnitOfPower.WATT,
        "icon": "mdi:flash",
        "precision": 0,
    },
    "rpm": {
        # HA has no fan-speed device class; the unit alone drives display.
        "device_class": None,
        "native_unit": REVOLUTIONS_PER_MINUTE,
        "icon": "mdi:fan",
        "precision": 0,
    },
    "percent": {
        "device_class": None,
        "native_unit": PERCENTAGE,
        "icon": "mdi:percent",
        "precision": 0,
    },
    "hz": {
        "device_class": SensorDeviceClass.FREQUENCY,
        "native_unit": UnitOfFrequency.HERTZ,
        "icon": "mdi:sine-wave",
        "precision": 0,
    },
    # Discrete sensors report a state, not a magnitude \u2014 no unit, no measurement.
    "unspecified": {},
    "discrete": {},
}

_WHITESPACE_RE = re.compile(r"\s+")


def _unit_config(unit: str) -> dict:
    """Resolve an ipmitool unit string to HA sensor properties.

    Matching is case-insensitive with whitespace collapsed, since BMCs are
    inconsistent about spelling ("degrees C" vs "Degrees  C").
    """
    if not unit:
        return {}
    normalized = _WHITESPACE_RE.sub(" ", unit).strip().lower()
    return SDR_UNIT_MAP.get(normalized, {})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IPMI SDR sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: IpmiDataUpdateCoordinator = data["coordinator"]

    sensors = entry.options.get(CONF_SENSORS, [])
    entities = [
        IpmiSdrSensor(coordinator, entry, sensor["name"], sensor.get("unit", ""))
        for sensor in sensors
    ]
    if entities:
        async_add_entities(entities)


class IpmiSdrSensor(
    CoordinatorEntity[IpmiDataUpdateCoordinator], SensorEntity
):
    """Sensor entity for any IPMI SDR sensor reading."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IpmiDataUpdateCoordinator,
        entry: ConfigEntry,
        sensor_name: str,
        unit: str,
    ) -> None:
        """Initialize the SDR sensor."""
        super().__init__(coordinator)
        self._sensor_name = sensor_name
        host_name = entry.data[CONF_HOST_NAME]
        safe_name = sensor_name.lower().replace(" ", "_")
        self._attr_unique_id = f"ipmi_{host_name}_{safe_name}"
        self._attr_name = sensor_name

        # Seed from the unit stored in config so the entity is correct immediately
        # after a restart, before the first poll. It may be empty — a sensor that was
        # unreadable when the config flow ran has no stored unit — in which case it is
        # learned from the first live reading that carries one.
        self._unit = unit or ""

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host_name)},
            name=f"IPMI {host_name.title()}",
            manufacturer="IPMI",
        )

        # The coordinator has already completed its first refresh by the time the
        # platform is set up, so a unit may be available right now.
        self._learn_unit()

    @property
    def _reading(self) -> dict | None:
        """Return the current raw reading dict for this sensor."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("sensor_readings", {}).get(self._sensor_name)

    def _learn_unit(self) -> None:
        """Adopt the unit from the live reading if one is not known yet.

        Only ever fills in a missing unit. A sensor that drops to "ns" reports an
        empty unit, and must not lose the one it already learned.
        """
        if self._unit:
            return
        reading = self._reading
        if reading and reading.get("unit"):
            self._unit = reading["unit"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Learn the unit from the live reading, then write state."""
        self._learn_unit()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return False for sensors the BMC reports no reading for.

        Unpopulated DIMM slots and absent drive-bay sensors sit permanently at "ns".
        Marking them unavailable rather than unknown keeps them out of history and
        long-term statistics.
        """
        if not super().available:
            return False
        reading = self._reading
        if reading is None:
            return False
        return reading.get("status") != STATUS_NO_READING

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return the device class implied by the resolved unit."""
        return _unit_config(self._unit).get("device_class")

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the resolved unit, falling back to the raw BMC string."""
        return _unit_config(self._unit).get("native_unit", self._unit or None)

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return MEASUREMENT only for sensors with a real numeric unit.

        Discrete sensors and those whose reading column is always blank carry no unit
        and must not be declared measurements.
        """
        if self.native_unit_of_measurement is None:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def suggested_display_precision(self) -> int | None:
        """Return the display precision implied by the resolved unit."""
        return _unit_config(self._unit).get("precision")

    @property
    def icon(self) -> str:
        """Return an icon matching the resolved unit."""
        return _unit_config(self._unit).get("icon", "mdi:chip")

    @property
    def native_value(self) -> float | None:
        """Return current sensor reading."""
        reading = self._reading
        if reading is None:
            return None
        return reading.get("value")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the BMC status plus any threshold values."""
        if self.coordinator.data is None:
            return None

        attrs: dict[str, Any] = {}

        reading = self._reading
        if reading and reading.get("status"):
            attrs["status"] = reading["status"]

        thresholds = self.coordinator.data.get("sensor_thresholds", {}).get(
            self._sensor_name
        )
        if thresholds:
            attrs.update(
                {
                    THRESHOLD_ATTR_MAP[key]: value
                    for key, value in thresholds.items()
                    if key in THRESHOLD_ATTR_MAP
                }
            )

        return attrs or None
