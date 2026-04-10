"""ipmitool subprocess wrapper with async execution."""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "8"))
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

_host_locks: dict[str, asyncio.Lock] = {}


def _get_host_lock(host: str) -> asyncio.Lock:
    """Get or create a per-host lock for serializing BMC requests."""
    if host not in _host_locks:
        _host_locks[host] = asyncio.Lock()
    return _host_locks[host]


async def run_ipmitool(
    host: str,
    user: str,
    password: str,
    privilege: str,
    *args: str,
    timeout: int = 15,
) -> tuple[str, str, int]:
    """Run an ipmitool command asynchronously.

    Per-host lock ensures one command at a time per BMC.
    Global semaphore caps total concurrent ipmitool processes.

    Returns (stdout, stderr, returncode).
    """
    async with _get_host_lock(host):
        async with SEMAPHORE:
            cmd = [
                "ipmitool", "-I", "lanplus",
                "-H", host, "-U", user, "-P", password,
                "-L", privilege,
                *args,
            ]
            logger.debug("Running: ipmitool -H %s -U %s -L %s %s", host, user, privilege, " ".join(args))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"ipmitool timed out after {timeout}s for {host}")

            stdout = stdout_bytes.decode().strip()
            stderr = stderr_bytes.decode().strip()
            return stdout, stderr, proc.returncode


def is_auth_error(stderr: str) -> bool:
    """Check if ipmitool stderr indicates an authentication failure.

    Only matches patterns that are genuinely auth/credential failures,
    verified against ipmitool source (lanplus.c, lan.c). The generic
    "unable to establish" message is excluded because it also fires
    for network timeouts and unreachable hosts.
    """
    lower = stderr.lower()
    return any(
        phrase in lower
        for phrase in [
            "rakp 2 hmac is invalid",
            "rakp 4 message has invalid integrity check value",
            "rakp 2 message indicates an error",
            "rakp 4 message indicates an error",
            "invalid user name",
            "insufficient privilege level",
            "requested privilege level exceeds limit",
            "invalid session authtype",
        ]
    )
