"""ipmitool subprocess wrapper with async execution."""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "8"))
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)


async def run_ipmitool(
    host: str,
    user: str,
    password: str,
    privilege: str,
    *args: str,
    timeout: int = 15,
) -> tuple[str, str, int]:
    """Run an ipmitool command asynchronously.

    Returns (stdout, stderr, returncode).
    """
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
    """Check if ipmitool stderr indicates an authentication failure."""
    lower = stderr.lower()
    return any(
        phrase in lower
        for phrase in [
            "unable to establish",
            "authentication type",
            "password invalid",
            "unauthorized",
            "insufficient privilege",
        ]
    )
