"""mDNS hostname publishing through avahi-publish.

Each published name maps the chosen .local hostname to this machine's
LAN IP on the local network. Avahi must be running (standard on most
Linux desktops); when it is not, publish() returns False and callers
fall back to IP-based URLs, so sharing itself never breaks.
"""

import re
import subprocess
from collections.abc import Callable

from utils.logger import get_logger

logger = get_logger("network")

LOCAL_SUFFIX = ".local"
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9-]+(\.local)?$")


def normalize_hostname(raw: str) -> str:
    """Lowercase and hyphenate user input, rejecting anything else.

    Spaces and underscores become hyphens so "My App" reads naturally;
    any other character outside [a-z0-9-.] raises ValueError so typos
    are surfaced instead of silently mangled.
    """
    cleaned = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if not HOSTNAME_PATTERN.match(cleaned):
        raise ValueError(f"Not a usable hostname: {raw!r}")
    return cleaned


def to_local_name(hostname: str) -> str:
    """Append the .local suffix unless it is already present."""
    if hostname.endswith(LOCAL_SUFFIX):
        return hostname
    return hostname + LOCAL_SUFFIX


class MdnsPublisher:
    """Owns one avahi-publish process per hostname."""

    def __init__(
        self, spawn: Callable[..., subprocess.Popen] = subprocess.Popen
    ) -> None:
        self._spawn = spawn
        self._processes: dict[str, subprocess.Popen] = {}

    def publish(self, hostname: str, ip: str) -> bool:
        """Announce hostname -> ip; return False when avahi is unusable."""
        self._terminate(hostname)
        try:
            process = self._spawn(
                ["avahi-publish", "-a", "-R", hostname, ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning(f"mDNS publish unavailable for {hostname}: {exc}")
            return False
        self._processes[hostname] = process
        logger.info(f"Published {hostname} -> {ip}")
        return True

    def unpublish(self, hostname: str) -> None:
        """Stop announcing a hostname; unknown names are ignored."""
        self._terminate(hostname)

    def stop_all(self) -> None:
        """Unpublish every hostname (app shutdown)."""
        for hostname in list(self._processes):
            self._terminate(hostname)

    def _terminate(self, hostname: str) -> None:
        """Kill and forget the process publishing hostname, if any."""
        process = self._processes.pop(hostname, None)
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except OSError:
            pass
        logger.info(f"Unpublished {hostname}")
