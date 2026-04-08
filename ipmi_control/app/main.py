"""FastAPI application for IPMI Control add-on."""

import logging
import os
import re

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .ipmi import is_auth_error, run_ipmitool

logger = logging.getLogger(__name__)

app = FastAPI(title="IPMI Control", version="2.0.0")


@app.on_event("startup")
async def register_discovery():
    """Register with Supervisor discovery so the integration can find us."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        logger.debug("No SUPERVISOR_TOKEN, skipping discovery registration")
        return

    hostname = os.environ.get("HOSTNAME", "")
    if not hostname:
        logger.warning("No HOSTNAME env var, cannot register discovery")
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://supervisor/discovery",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "service": "ipmi_control",
                    "config": {
                        "host": hostname,
                        "port": 8099,
                    },
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Registered with Supervisor discovery as %s:8099", hostname)
            else:
                logger.warning("Discovery registration returned %s: %s", resp.status_code, resp.text)
    except Exception as err:
        logger.warning("Failed to register with Supervisor discovery: %s", err)


# --- Request models ---


class IpmiCredentials(BaseModel):
    host: str
    user: str
    password: str


class ChassisStatusRequest(IpmiCredentials):
    pass


class ChassisPowerRequest(IpmiCredentials):
    action: str  # "on" or "soft"


class RawCommandRequest(IpmiCredentials):
    privilege: str = "ADMINISTRATOR"
    command: str  # e.g., "raw 0x30 0x45 0x00"


class SensorThresholdsGetRequest(IpmiCredentials):
    sensor_name: str


class SensorThresholdsSetRequest(IpmiCredentials):
    sensor_name: str
    lower: list[int] | None = None  # [LNR, LC, LNC]
    upper: list[int] | None = None  # [UNC, UC, UNR]


# --- Helper ---


def _check_error(stderr: str, returncode: int) -> None:
    """Raise appropriate HTTPException on ipmitool error."""
    if returncode == 0:
        return
    if is_auth_error(stderr):
        raise HTTPException(status_code=401, detail=f"Authentication failed: {stderr}")
    raise HTTPException(status_code=500, detail=f"ipmitool error: {stderr}")


def _parse_sdr_line(line: str) -> dict | None:
    """Parse a single SDR elist full output line.

    Format: "CPU Temp         | 01h | ok  |  3.1 | 42 degrees C"
    Returns: {"name": "CPU Temp", "value": 42.0, "unit": "degrees C", "status": "ok"}
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split("|")
    if len(parts) < 5:
        return None
    name = parts[0].strip()
    if not name:
        return None
    status = parts[2].strip()
    reading = parts[4].strip()

    value = None
    unit = ""
    if status == "ok" or status == "cr":
        match = re.match(r"([\d.]+)\s+(.*)", reading)
        if match:
            value = float(match.group(1))
            unit = match.group(2).strip()

    return {"name": name, "value": value, "unit": unit, "status": status}


# --- Endpoints ---


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/chassis/status")
async def chassis_status(req: ChassisStatusRequest):
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "chassis", "status",
    )
    _check_error(stderr, rc)

    power = None
    for line in stdout.split("\n"):
        if "System Power" in line:
            power = "on" in line.lower()
            break

    if power is None:
        raise HTTPException(status_code=500, detail=f"Could not parse chassis status: {stdout}")

    return {"power": power}


@app.post("/api/chassis/status/admin")
async def chassis_status_admin(req: ChassisStatusRequest):
    """Verify credentials have Administrator privilege."""
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "ADMINISTRATOR",
        "chassis", "status",
    )
    _check_error(stderr, rc)
    return {"ok": True}


@app.post("/api/chassis/power")
async def chassis_power(req: ChassisPowerRequest):
    if req.action not in ("on", "soft"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {req.action}")

    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "chassis", "power", req.action,
    )
    _check_error(stderr, rc)
    return {"ok": True}


@app.post("/api/raw")
async def raw_command(req: RawCommandRequest):
    parts = req.command.split()
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, req.privilege,
        *parts,
    )
    _check_error(stderr, rc)
    return {"output": stdout}


@app.post("/api/sensor/thresholds/get")
async def sensor_thresholds_get(req: SensorThresholdsGetRequest):
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "sensor", "get", req.sensor_name,
    )
    _check_error(stderr, rc)

    thresholds = {}
    threshold_keys = {
        "Lower Non-Recoverable": "lnr",
        "Lower Critical": "lc",
        "Lower Non-Critical": "lnc",
        "Upper Non-Critical": "unc",
        "Upper Critical": "uc",
        "Upper Non-Recoverable": "unr",
    }

    for line in stdout.split("\n"):
        for label, key in threshold_keys.items():
            if label in line:
                match = re.search(r":\s*([\d.]+)", line)
                if match:
                    thresholds[key] = int(float(match.group(1)))

    return thresholds


@app.post("/api/sensor/thresholds/set")
async def sensor_thresholds_set(req: SensorThresholdsSetRequest):
    results = []

    if req.lower and len(req.lower) >= 3:
        stdout, stderr, rc = await run_ipmitool(
            req.host, req.user, req.password, "ADMINISTRATOR",
            "sensor", "thresh", req.sensor_name, "lower",
            str(req.lower[0]), str(req.lower[1]), str(req.lower[2]),
            timeout=10,
        )
        _check_error(stderr, rc)
        results.append("lower")

    if req.upper and len(req.upper) >= 3:
        stdout, stderr, rc = await run_ipmitool(
            req.host, req.user, req.password, "ADMINISTRATOR",
            "sensor", "thresh", req.sensor_name, "upper",
            str(req.upper[0]), str(req.upper[1]), str(req.upper[2]),
            timeout=10,
        )
        _check_error(stderr, rc)
        results.append("upper")

    return {"ok": True, "set": results}


@app.post("/api/sdr/list")
async def sdr_list(req: IpmiCredentials):
    """Return all SDR sensor names and units for discovery."""
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "sdr", "elist", "full",
    )
    _check_error(stderr, rc)

    sensors = []
    for line in stdout.split("\n"):
        parsed = _parse_sdr_line(line)
        if parsed:
            sensors.append({"name": parsed["name"], "unit": parsed["unit"]})

    return {"sensors": sensors}


@app.post("/api/sdr/readings")
async def sdr_readings(req: IpmiCredentials):
    """Return current readings for all SDR sensors."""
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "sdr", "elist", "full",
    )
    _check_error(stderr, rc)

    sensors = {}
    for line in stdout.split("\n"):
        parsed = _parse_sdr_line(line)
        if parsed:
            sensors[parsed["name"]] = {
                "value": parsed["value"],
                "unit": parsed["unit"],
                "status": parsed["status"],
            }

    return {"sensors": sensors}
