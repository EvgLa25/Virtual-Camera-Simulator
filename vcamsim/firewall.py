"""Windows Firewall helper.

The simulator binds 0.0.0.0, but Windows Firewall blocks inbound connections
from *other hosts* by default -- so ONVIF/RTSP works from the local machine and
silently fails from a VMS server on another IP.  This adds an inbound allow
rule for the running interpreter/executable.
"""
from __future__ import annotations

import subprocess
import sys

RULE = "VCamSim"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _netsh(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(["netsh"] + args, capture_output=True,
                           creationflags=_NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except FileNotFoundError:
        return 1, "netsh not found"


def program_path() -> str:
    """The binary Windows Firewall will see listening."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable            # python.exe / pythonw.exe


def rule_exists() -> bool:
    rc, out = _netsh(["advfirewall", "firewall", "show", "rule", f"name={RULE}"])
    return rc == 0 and RULE in out


def add_rule() -> tuple[bool, str]:
    exe = program_path()
    _netsh(["advfirewall", "firewall", "delete", "rule", f"name={RULE}"])
    ok = True
    msgs = []
    for proto in ("TCP", "UDP"):
        rc, out = _netsh([
            "advfirewall", "firewall", "add", "rule", f"name={RULE}",
            "dir=in", "action=allow", f"program={exe}",
            f"protocol={proto}", "enable=yes", "profile=any"])
        if rc != 0:
            ok = False
            msgs.append(out.strip()[:160])
    if ok:
        return True, f"inbound TCP+UDP allowed for {exe}"
    return False, "; ".join(msgs) or "failed (Administrator required)"


def remove_rule() -> tuple[bool, str]:
    rc, out = _netsh(["advfirewall", "firewall", "delete", "rule", f"name={RULE}"])
    return rc == 0, out.strip()[:200]
