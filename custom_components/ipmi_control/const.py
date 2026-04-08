"""Constants for the IPMI Control integration."""

from __future__ import annotations

DOMAIN = "ipmi_control"

# Config entry data keys
CONF_ADDON_URL = "addon_url"
CONF_HOST_NAME = "host"
CONF_IPMI_IP = "ip"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PRIVILEGE_LEVEL = "privilege_level"

# Config entry options keys
CONF_POWER_CONTROL = "power_control"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MOTHERBOARD = "motherboard"
CONF_FAN_MODES = "fan_modes"
CONF_FAN_MODE_COMMANDS = "fan_mode_commands"
CONF_FAN_MODE_QUERY_COMMAND = "fan_mode_query_command"
CONF_FAN_MODE_RESPONSE_MAPPING = "fan_mode_response_mapping"
CONF_FAN_MODE_DISPLAY_MAPPING = "fan_mode_display_mapping"
CONF_VIRTUAL_MODE_MAPPING = "virtual_mode_mapping"
CONF_SENSORS = "sensors"
CONF_SELECTED_SENSORS = "selected_sensors"
CONF_THRESHOLD_SENSORS = "threshold_sensors"

# Sensor threshold field keys
CONF_THRESH_LNR = "thresh_lnr"
CONF_THRESH_LC = "thresh_lc"
CONF_THRESH_LNC = "thresh_lnc"
CONF_THRESH_UNC = "thresh_unc"
CONF_THRESH_UC = "thresh_uc"
CONF_THRESH_UNR = "thresh_unr"

# Power control actions (stored as list in CONF_POWER_CONTROL)
POWER_ON = "on"
POWER_SOFT_OFF = "soft_off"
POWER_HARD_OFF = "hard_off"

# Hard power off safety gate
CONF_HARD_OFF_DISARM_TIMEOUT = "hard_off_disarm_timeout"
DEFAULT_HARD_OFF_DISARM_TIMEOUT = 30

# Defaults
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_POWER_CONTROL = [POWER_ON, POWER_SOFT_OFF]
CONF_POWER_STATE_HOLD = "power_state_hold"
DEFAULT_POWER_STATE_HOLD = 60

# Motherboard profiles
MOTHERBOARD_NONE = "none"

MOTHERBOARD_PROFILES: dict[str, dict] = {
    "supermicro": {
        "fan_modes": ["standard", "full", "optimum", "heavy_io"],
        "fan_mode_display_mapping": {
            "standard": "Standard",
            "full": "Full",
            "optimum": "Optimum",
            "heavy_io": "Heavy IO",
        },
        "fan_mode_query_command": {
            "netfn": 0x30,
            "command": 0x45,
            "data": [0x00],
        },
        "fan_mode_response_mapping": {
            0x00: "standard",
            0x01: "full",
            0x02: "optimum",
            0x04: "heavy_io",
        },
        "fan_mode_commands": {
            "standard": [{"netfn": 0x30, "command": 0x45, "data": [0x01, 0x00]}],
            "full": [{"netfn": 0x30, "command": 0x45, "data": [0x01, 0x01]}],
            "optimum": [{"netfn": 0x30, "command": 0x45, "data": [0x01, 0x02]}],
            "heavy_io": [{"netfn": 0x30, "command": 0x45, "data": [0x01, 0x04]}],
        },
    },
}

# Metis-specific quiet mode extension (on top of supermicro profile)
METIS_QUIET_MODE = {
    "mode_name": "quiet",
    "display_name": "Quiet",
    "virtual_maps_to": "standard",
    "commands": [
        {"netfn": 0x30, "command": 0x45, "data": [0x01, 0x00]},
        {"netfn": 0x30, "command": 0x70, "data": [0x66, 0x01, 0x00, 0x32]},
        {"netfn": 0x30, "command": 0x70, "data": [0x66, 0x01, 0x01, 0x32]},
    ],
}
