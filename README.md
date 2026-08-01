<p align="center">
  <img src="https://raw.githubusercontent.com/metril/ha-ipmi-control/main/custom_components/ipmi_control/brand/logo.png" alt="IPMI Control" width="312">
</p>

# IPMI Control for Home Assistant

A Home Assistant custom integration + add-on to manage server power, fan control, and sensor monitoring via IPMI. Uses a companion add-on running `ipmitool` for reliable BMC communication.

## Features

- **Power Control** — Turn servers on/off with configurable policies (both, on-only, off-only, disabled)
- **General Sensor Support** — Expose any BMC sensor (temperature, voltage, fan speed, power, current) with automatic device class mapping
- **Fan Mode Control** — Switch between fan modes (Standard, Full, Optimum, Heavy IO, custom/virtual modes) on Supermicro boards
- **Sensor Thresholds** — View and configure thresholds for any sensor, applied via button press
- **BMC Cold Reset** — Reboot a wedged BMC without touching the server's power (Administrator only, arm-then-press)
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

Select which BMC sensors to expose in Home Assistant. All sensor types are supported (temperature, voltage, fan, power, current). Sensors the BMC currently reports no reading for — unpopulated DIMM slots, empty drive bays — are labelled `(no reading)`. Manual entry available as fallback.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Power | Switch | Turn the server on/off (respects power control policy) |
| Power State | Binary Sensor | Shows whether the server is powered on |
| Fan Mode | Select | Switch between configured fan modes (Administrator only) |
| Set Sensor Thresholds | Button | Apply configured threshold overrides to the BMC (Administrator only) |
| Refresh Sensor Thresholds | Button | Re-read current thresholds from the BMC (diagnostic) |
| BMC Reset (Arm) | Switch | Arms the BMC cold reset for a short window (Administrator only) |
| BMC Cold Reset | Button | Reboots the BMC itself; refuses unless armed (Administrator only) |
| *Per-sensor* | Sensor | Temperature (°C/°F/K), voltage (V), fan speed (rpm), power (W), current (A), percent, frequency (Hz) |

### BMC cold reset

When a BMC wedges — SDR queries hang, the web UI stops responding, fan commands are
ignored — a cold reset reboots the management controller without touching the server's
power state. The running OS is unaffected.

It is destructive enough to be gated twice:

1. **Administrator credentials.** Operator entries never get the entities at all, the same
   way they never get the Fan Mode select.
2. **Arm, then press.** Turn on **BMC Reset (Arm)**, then press **BMC Cold Reset** within
   the auto-disarm timeout (default 30 s). Pressing while unarmed raises an error instead
   of resetting anything.

The BMC drops every IPMI session and is unreachable for roughly a minute afterwards. The
integration expects this: for the configured **BMC reset grace period** (default 120 s) it
keeps polling but treats connection failures as normal, so entities hold their last known
state instead of flapping to unavailable and the log stays quiet. The window closes as
soon as a poll succeeds.

For automations, the `ipmi_control.bmc_cold_reset` action takes the button's `entity_id`
plus `confirm`. `confirm: true` skips the arm switch:

```yaml
action: ipmi_control.bmc_cold_reset
data:
  entity_id: button.ipmi_node7_bmc_cold_reset
  confirm: true
```

### Sensor attributes

Every SDR sensor exposes a `status` attribute carrying the raw BMC state — `ok`, `ns` (no reading), or a threshold state such as `lnc` / `unc` / `cr`. A fan reading a valid RPM while sitting below its lower non-critical threshold reports `status: lnc`, which is otherwise invisible from the value alone:

```yaml
# Alert on any sensor that has left its normal range
{{ state_attr('sensor.ipmi_node7_fan_3', 'status') not in ['ok', 'ns'] }}
```

Sensors with BMC thresholds also show them as attributes: `lower_non_recoverable`, `lower_critical`, `lower_non_critical`, `upper_non_critical`, `upper_critical`, `upper_non_recoverable`.

Sensors reporting `ns` are marked **unavailable** rather than unknown, so unpopulated DIMM slots and absent drive bays stay out of history graphs and long-term statistics.

### Units

Units come from the BMC and are mapped to Home Assistant device classes automatically. A sensor that was unreadable when you first configured it is stored without a unit; the integration learns the unit from the first live reading that carries one and writes it back to the config entry, so no reconfiguration is needed. An already-known unit is never cleared — a fan dropping to `ns` keeps its `rpm`.

Unrecognized unit strings are passed through to Home Assistant verbatim with no device class.

## Privilege Levels

- **Administrator** — Full access: power control, fan mode, sensor reading, threshold setting, BMC cold reset
- **Operator** — Power control and sensor reading. Fan mode select, threshold buttons, and the BMC cold reset entities are not created.

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
