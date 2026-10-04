"""Runtime data for IPMI Controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import IpmiDataUpdateCoordinator
    from .ipmi import IpmiClient


@dataclass
class IpmiRuntimeData:
    """Per-entry runtime state."""

    coordinator: IpmiDataUpdateCoordinator
    client: IpmiClient
    hard_off_armed: bool = False
    bmc_reset_armed: bool = False
    # monotonic deadline; while in the future the coordinator treats
    # connection failures as the BMC rebooting rather than as errors
    bmc_reset_grace_until: float | None = None


type IpmiConfigEntry = ConfigEntry[IpmiRuntimeData]
