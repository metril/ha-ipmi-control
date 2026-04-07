"""FastAPI application for IPMI Control add-on."""

import logging
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .ipmi import is_auth_error, run_ipmitool

logger = logging.getLogger(__name__)

app = FastAPI(title="IPMI Control", version="1.0.0")


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


class SdrFansRequest(IpmiCredentials):
    pass


# --- Helper ---


def _check_error(stderr: str, returncode: int) -> None:
    """Raise appropriate HTTPException on ipmitool error."""
    if returncode == 0:
        return
    if is_auth_error(stderr):
        raise HTTPException(status_code=401, detail=f"Authentication failed: {stderr}")
    raise HTTPException(status_code=500, detail=f"ipmitool error: {stderr}")


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


@app.post("/api/sdr/fans")
async def sdr_fans(req: SdrFansRequest):
    stdout, stderr, rc = await run_ipmitool(
        req.host, req.user, req.password, "OPERATOR",
        "sdr", "type", "Fan",
    )
    _check_error(stderr, rc)

    fans = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Format: "FAN 1            | 30h | ok  |  7.1 | 3400 RPM"
        parts = line.split("|")
        if parts:
            fan_name = parts[0].strip()
            if fan_name:
                fans.append(fan_name)

    return {"fans": fans}
