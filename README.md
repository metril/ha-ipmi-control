# IPMI Control for Home Assistant

A Home Assistant custom integration + add-on to manage server power, fan control, and sensor monitoring via IPMI. Uses a companion add-on running `ipmitool` for reliable BMC communication.

## Features

- **Power Control** — Turn servers on/off with configurable policies (both, on-only, off-only, disabled)
- **General Sensor Support** — Expose any BMC sensor (temperature, voltage, fan speed, power, current) with automatic device class mapping
- **Fan Mode Control** — Switch between fan modes (Standard, Full, Optimum, Heavy IO, custom/virtual modes) on Supermicro boards
- **Sensor Thresholds** — View and configure thresholds for any sensor, applied via button press
- **Single Credential Model** — One username/password with privilege level selection (Administrator or Operator)
- **Per-Host Serialization** — BMC requests are serialized per-host to prevent session conflicts
- **On-Demand Threshold Refresh** — Thresholds fetched once on startup, refreshed via diagnostic button
- **Config Flow** — Full UI-based setup with auto-detection of add-on URL
- **Reauth & Reconfigure** — Update credentials or IP addresses without removing the integration
- **Options Flow** — Adjust power policy, poll interval, fan config, sensor selection, and thresholds at any time
- **Diagnostics** — Export redacted config for troubleshooting

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Search for "IPMI Control" and install
3. Install the IPMI Control add-on from the add-on store
4. Restart Home Assistant
5. Go to **Settings > Devices & Services > Add Integration** and search for "IPMI Control"

### Manual

1. Copy `custom_components/ipmi_control/` to your Home Assistant `config/custom_components/` directory
2. Install and start the IPMI Control add-on
3. Restart Home Assistant
4. Add the integration via the UI

## Configuration

Each IPMI host is added as a separate integration entry. The setup flow has four steps:

### Step 1: Connection

| Field | Description |
|-------|-------------|
| **Host name** | A short name for this host (e.g., `menoetius`) |
| **BMC IP address** | IP address of the IPMI/BMC interface |
| **Username** | IPMI username for BMC access |
| **Password** | IPMI password |
| **Privilege level** | Administrator (full control) or Operator (read-only) |

The add-on URL is auto-detected via Supervisor.

### Step 2: Power Control

| Field | Description |
|-------|-------------|
| **Power control policy** | `both`, `on`, `off`, or `none` |
| **Poll interval** | How often to query the BMC (5-300 seconds, default: 10) |

### Step 3: Fan Profile

Select a motherboard profile to pre-fill fan mode commands:
- **Supermicro** — Standard, Full, Optimum, Heavy IO modes
- **None** — Skip fan control entirely

### Step 4: Sensor Selection

Select which BMC sensors to expose in Home Assistant. All sensor types are supported (temperature, voltage, fan, power, current). Manual entry available as fallback.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Power | Switch | Turn the server on/off (respects power control policy) |
| Power State | Binary Sensor | Shows whether the server is powered on |
| Fan Mode | Select | Switch between configured fan modes (Administrator only) |
| Set Sensor Thresholds | Button | Apply configured threshold overrides to the BMC (Administrator only) |
| Refresh Sensor Thresholds | Button | Re-read current thresholds from the BMC (diagnostic) |
| *Per-sensor* | Sensor | Temperature (°C), voltage (V), fan speed (RPM), power (W), current (A) |

All sensors with BMC thresholds show them as attributes: `lower_non_recoverable`, `lower_critical`, `lower_non_critical`, `upper_non_critical`, `upper_critical`, `upper_non_recoverable`.

## Privilege Levels

- **Administrator** — Full access: power control, fan mode, sensor reading, threshold setting
- **Operator** — Read-only: power control, sensor reading. Fan mode select and threshold buttons are not created.

## Virtual Fan Modes

You can define virtual modes that map to an underlying IPMI mode but execute additional commands. For example, a "Quiet" mode that sets Standard mode plus custom fan speed limits.

Virtual modes are configured in the options flow via the `virtual_mode_mapping` setting.

## Architecture

```
HA Core (integration) --HTTP--> Add-on (FastAPI + ipmitool) --IPMI--> BMC
```

The integration communicates with the add-on via HTTP on HA's internal Docker network. The add-on is stateless — credentials are sent per-request, no persistence. Per-host locks ensure one ipmitool call per BMC at a time.

## Requirements

- Home Assistant OS (HAOS)
- IPMI Control add-on installed and running
- An IPMI-capable server with BMC accessible over the network
- IPMI credentials (Administrator recommended, Operator for read-only)
