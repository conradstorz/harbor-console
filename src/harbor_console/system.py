"""System metrics collection for Harbor Console."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable
from datetime import datetime

import psutil

from harbor_console.docker import DOCKER_TIMEOUT_SECONDS


def format_uptime(total_seconds: int) -> str:
    """Format uptime seconds as d HH:MM:SS."""
    days, rem = divmod(max(total_seconds, 0), 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"


def format_bytes(n: int) -> str:
    """Gibibytes with one decimal, as `free -h` counts them.

    Returns the number alone, without a unit: a summary line names `GiB` once
    for the pair rather than twice.
    """
    return f"{n / 1024**3:.1f}"


def format_usage(used: int, total: int, percent: float) -> str:
    """`used / total GiB (percent)` -- the shape both memory and swap take."""
    return f"{format_bytes(used)} / {format_bytes(total)} GiB ({percent:.1f}%)"


def get_swap_summary(
    swap_memory: Callable[[], object] = psutil.swap_memory,
) -> str:
    """Swap usage, degrading rather than raising.

    `psutil.swap_memory()` can fail outright on some platforms, and a
    collector here never raises on a hostile environment. A host with no swap
    says so rather than reading `0.0 / 0.0 GiB (0.0%)`, which looks like a bug
    rather than a fact.

    The guard covers only the call to `swap_memory()` -- the platform failure
    it exists for. A missing or renamed attribute on the object it returns is
    a bug in this code, not a platform limitation, and should surface as one
    rather than being swallowed and reported as "unavailable".
    """
    try:
        swap = swap_memory()
    except Exception:
        return "unavailable"
    total = int(swap.total)  # type: ignore[attr-defined]
    used = int(swap.used)  # type: ignore[attr-defined]
    percent = float(swap.percent)  # type: ignore[attr-defined]
    if total == 0:
        return "none configured"
    return format_usage(used, total, percent)


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


def get_docker_container_count(
    run: Callable[..., object] = subprocess.run,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> int:
    """Return the number of running Docker containers.

    Bounded for the same reason `docker.running_containers` is: this runs in
    the web prober thread every cycle, and a wedged daemon with no bound
    blocks that thread forever while the last snapshot is served as current.
    """
    try:
        result = run(
            ["docker", "ps", "-q"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 0
    except (FileNotFoundError, OSError):
        return 0

    if result.returncode != 0:  # type: ignore[attr-defined]
        return 0

    lines = [line for line in result.stdout.splitlines() if line.strip()]  # type: ignore[attr-defined]
    return len(lines)


def collect_system_metrics() -> dict[str, str | float | int]:
    """Collect all metrics required for Harbor Console MVP."""
    now = datetime.now()
    uptime_seconds = int(now.timestamp() - psutil.boot_time())
    memory = psutil.virtual_memory()

    return {
        "hostname": socket.gethostname(),
        "uptime": format_uptime(uptime_seconds),
        "cpu_utilization": psutil.cpu_percent(interval=None),
        # Used is total - available, the basis psutil's own `percent` uses, so
        # the bytes and the percentage on one line agree with each other.
        "memory_summary": format_usage(
            memory.total - memory.available, memory.total, memory.percent
        ),
        "swap_summary": get_swap_summary(),
        "ipv4_address": get_ipv4_address(),
        "docker_container_count": get_docker_container_count(),
        "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
