"""Constants for the IPMI Controller integration."""

from __future__ import annotations

DOMAIN = "ipmi_controller"

# Config entry data keys
CONF_ADDON_URL = "addon_url"
CONF_HOST_NAME = "host"
CONF_IPMI_IP = "ip"
CONF_OPERATOR_USER = "operator_username"
CONF_OPERATOR_PASS = "operator_password"
CONF_ADMIN_USER = "admin_username"
CONF_ADMIN_PASS = "admin_password"

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
CONF_FANS = "fans"
CONF_SELECTED_FANS = "selected_fans"

# Fan threshold field keys
CONF_FAN_LNR = "fan_lnr"
CONF_FAN_LC = "fan_lc"
CONF_FAN_LNC = "fan_lnc"
CONF_FAN_UNC = "fan_unc"
CONF_FAN_UC = "fan_uc"
CONF_FAN_UNR = "fan_unr"

# Power control policies
POWER_CONTROL_BOTH = "both"
POWER_CONTROL_ON = "on"
POWER_CONTROL_OFF = "off"
POWER_CONTROL_NONE = "none"

# Defaults
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_POWER_CONTROL = POWER_CONTROL_BOTH

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
