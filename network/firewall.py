"""Help for the most common reason a phone cannot connect: the firewall.

A connection test from this computer cannot detect a firewall (local
traffic never crosses it), so Xposer explains the fix per platform and,
on Windows, can add the inbound rule itself after a UAC prompt.
"""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from utils.logger import get_logger
from utils.platform_env import IS_LINUX, IS_MACOS, IS_WINDOWS, WINDOWS_GUI_INTERPRETER

logger = get_logger("firewall")

WINDOWS_RULE_PREFIX = "Xposer"
WINDOWS_CONSOLE_INTERPRETER = "python.exe"
SHELL_EXECUTE_SUCCESS_MINIMUM = 32
SW_HIDE = 0


@dataclass(frozen=True)
class FirewallHelp:
    """Platform-specific advice for letting LAN devices connect."""

    summary: str
    steps: tuple[str, ...]
    can_auto_allow: bool = False


def firewall_help(port: int | None = None) -> FirewallHelp:
    """Advice for this platform; port makes Linux commands copy-pasteable."""
    common = (
        "Make sure the phone is on the same Wi-Fi network as this computer "
        "(guest networks often isolate devices)."
    )
    if IS_WINDOWS:
        return FirewallHelp(
            summary="Windows Firewall blocks phones until Python is allowed on private networks.",
            steps=(
                "Click “Allow through firewall” and accept the Windows prompt.",
                "Set your Wi-Fi to a Private network: Settings → Network & internet → "
                "Wi-Fi → your network → Private network.",
                common,
            ),
            can_auto_allow=True,
        )
    if IS_MACOS:
        return FirewallHelp(
            summary="macOS may ask whether Python can accept incoming connections.",
            steps=(
                "Choose Allow when macOS asks, or add Python in System Settings → "
                "Network → Firewall → Options.",
                common,
            ),
        )
    target = str(port) if port is not None else "<port>"
    steps: list[str] = []
    if IS_LINUX and shutil.which("ufw"):
        steps.append(f"ufw: sudo ufw allow {target}/tcp")
    if IS_LINUX and shutil.which("firewall-cmd"):
        steps.append(f"firewalld: sudo firewall-cmd --add-port={target}/tcp")
    if not steps:
        steps.append(f"If a firewall is enabled, allow incoming TCP port {target}.")
    steps.append(common)
    return FirewallHelp(
        summary="A firewall on this computer may be blocking other devices.",
        steps=tuple(steps),
    )


def windows_interpreters() -> list[Path]:
    """python.exe and pythonw.exe of the running installation (those that exist)."""
    current = Path(sys.executable)
    candidates = [current]
    for name in (WINDOWS_CONSOLE_INTERPRETER, WINDOWS_GUI_INTERPRETER):
        sibling = current.with_name(name)
        if sibling not in candidates:
            candidates.append(sibling)
    return [path for path in candidates if path.exists()]


def windows_rule_commands(programs: list[Path]) -> str:
    """One cmd.exe line replacing Xposer's inbound rules for programs."""
    commands: list[str] = []
    for program in programs:
        rule = f"{WINDOWS_RULE_PREFIX} - {program.name}"
        commands.append(f'netsh advfirewall firewall delete rule name="{rule}"')
        commands.append(
            f'netsh advfirewall firewall add rule name="{rule}" dir=in action=allow '
            f'program="{program}" enable=yes profile=private,domain'
        )
    return " & ".join(commands)


def allow_through_windows_firewall() -> bool:
    """Show the UAC prompt that adds inbound rules for this Python.

    Returns True when Windows started the elevated command (the user
    accepted the prompt); False when declined or not on Windows.
    """
    if not IS_WINDOWS:
        return False
    programs = windows_interpreters()
    if not programs:
        return False
    import ctypes

    parameters = f"/c {windows_rule_commands(programs)}"
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", parameters, None, SW_HIDE
    )
    accepted = int(result) > SHELL_EXECUTE_SUCCESS_MINIMUM
    logger.info(f"Firewall rule request {'accepted' if accepted else 'declined'} ({result})")
    return accepted
