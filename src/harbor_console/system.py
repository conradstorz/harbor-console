"""System metrics collection for Harbor Console."""

from __future__ import annotations

import socket
import subprocess
from datetime import datetime

import psutil


def format_uptime(total_seconds: int) -> str:
    """Format uptime seconds as d HH:MM:SS."""
    days, rem = divmod(max(total_seconds, 0), 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"


def get_ipv4_address() -> str:
    """Return primary IPv4 address for the host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def get_docker_container_count() -> int:
    """Return the number of running Docker containers."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return 0

    if result.returncode != 0:
        return 0

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return len(lines)


def collect_system_metrics() -> dict[str, str | float | int]:
    """Collect all metrics required for Harbor Console MVP."""
    now = datetime.now()
    uptime_seconds = int(now.timestamp() - psutil.boot_time())

    return {
        "hostname": socket.gethostname(),
        "uptime": format_uptime(uptime_seconds),
        "cpu_utilization": psutil.cpu_percent(interval=None),
        "memory_utilization": psutil.virtual_memory().percent,
        "disk_utilization": psutil.disk_usage("/").percent,
        "ipv4_address": get_ipv4_address(),
        "docker_container_count": get_docker_container_count(),
        "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
