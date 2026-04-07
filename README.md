# IPMI Controller for Home Assistant

A Home Assistant custom integration to manage server power and fan control via IPMI. Uses pure Python (pyghmi) for IPMI 2.0 communication — no `ipmitool` binary required, fully compatible with HAOS.

## Features

- **Power Control** — Turn servers on/off with configurable policies (both, on-only, off-only, disabled)
- **Fan Mode Control** — Switch between fan modes (Standard, Full, Optimum, Heavy IO, custom/virtual modes)
- **Fan Threshold Management** — Apply fan sensor thresholds with a single button press
- **Motherboard Profiles** — Built-in Supermicro profile with pre-configured fan mode commands
- **Virtual Mode Mapping** — Define custom modes that map to underlying IPMI modes with additional commands
- **Config Flow** — Full UI-based setup with multi-step configuration
- **Reauth & Reconfigure** — Update credentials or IP addresses without removing the integration
- **Options Flow** — Adjust power policy, poll interval, fan config, and thresholds at any time
- **Diagnostics** — Export redacted config for troubleshooting

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Search for "IPMI Controller" and install
3. Restart Home Assistant
4. Go to **Settings > Devices & Services > Add Integration** and search for "IPMI Controller"

### Manual

1. Copy `custom_components/ipmi_controller/` to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration via the UI

## Configuration

Each IPMI host is added as a separate integration entry. The setup flow has four steps:

### Step 1: Connection

| Field | Description |
|-------|-------------|
| **Host name** | A short name for this host (e.g., `menoetius`) |
| **BMC IP address** | IP address of the IPMI/BMC interface |
| **Operator username/password** | Used for chassis power operations |
| **Administrator username/password** | Used for fan control and threshold operations |

### Step 2: Power Control

| Field | Description |
|-------|-------------|
| **Power control policy** | `both`, `on`, `off`, or `none` |
| **Poll interval** | How often to query the BMC (5-300 seconds, default: 10) |

### Step 3: Fan Profile

Select a motherboard profile to pre-fill fan mode commands:
- **Supermicro** — Standard, Full, Optimum, Heavy IO modes
- **None** — Skip fan control entirely

### Step 4: Fan Sensors

Configure fan sensors and thresholds using the format:

```
FAN1:75,150,225:3150,3300,3450;FAN2:100,200,300:2200,2300,2400
```

Format: `NAME:LNR,LC,LNC:UNC,UC,UNR` separated by `;`

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Power | Switch | Turn the server on/off (respects power control policy) |
| Power State | Binary Sensor | Shows whether the server is powered on |
| Fan Mode | Select | Switch between configured fan modes |
| Set Fan Thresholds | Button | Apply all configured fan thresholds to the BMC |

## Virtual Fan Modes

You can define virtual modes that map to an underlying IPMI mode but execute additional commands. For example, a "Quiet" mode that sets Standard mode plus custom fan speed limits.

Virtual modes are configured in the options flow via the `virtual_mode_mapping` setting.

## Requirements

- An IPMI-capable server with BMC accessible over the network
- IPMI credentials (Operator level for power, Administrator level for fan control)
- No system dependencies — uses pyghmi for pure Python IPMI 2.0
