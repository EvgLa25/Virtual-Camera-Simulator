"""Query and control the Windows service from outside it.

Everything goes through `sc.exe` rather than pywin32 so the GUI does not need
pywin32 at all, and so it works identically against the installed
VCamSimSvc.exe and a service registered by hand.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .service import SERVICE_DISPLAY, SERVICE_NAME

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Set VCAMSIM_NO_SERVICE=1 to make the app ignore an installed service and run
# its own in-process engine. Without it, a test (or a second copy of the GUI)
# on a machine that has the service installed silently attaches to it and
# adopts its config.
NO_SERVICE_ENV = "VCAMSIM_NO_SERVICE"


def disabled() -> bool:
    return os.environ.get(NO_SERVICE_ENV, "").strip().lower() in ("1", "true", "yes")

# sc.exe reports these; map to something worth showing a user
STATES = {
    "STOPPED": "stopped", "START_PENDING": "starting",
    "STOP_PENDING": "stopping", "RUNNING": "running",
    "CONTINUE_PENDING": "starting", "PAUSE_PENDING": "stopping",
    "PAUSED": "paused",
}


def _sc(*args: str) -> tuple[int, str]:
    if sys.platform != "win32" or disabled():
        return 1, "not Windows"
    try:
        r = subprocess.run(["sc"] + list(args), capture_output=True,
                           creationflags=_NO_WINDOW, timeout=30)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except FileNotFoundError:
        return 1, "sc.exe not found"
    except subprocess.TimeoutExpired:
        return 1, "sc.exe timed out"


def service_exe() -> Path | None:
    """VCamSimSvc.exe beside the GUI exe (installed layout)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    for cand in (base / "VCamSimSvc.exe", base / "dist" / "VCamSimSvc.exe"):
        if cand.exists():
            return cand
    return None


# sc.exe exits with the Win32 error code, which is the only thing that is not
# localized: the message text is in the OS language.
ERR_ACCESS_DENIED = 5
ERR_SERVICE_ALREADY_RUNNING = 1056
ERR_SERVICE_DOES_NOT_EXIST = 1060
ERR_SERVICE_NOT_ACTIVE = 1062
ERR_SERVICE_EXISTS = 1073
ERR_SERVICE_MARKED_FOR_DELETE = 1072


def _explain(rc: int, out: str) -> str:
    if rc == ERR_ACCESS_DENIED:
        return "access denied -- Administrator rights are required"
    if rc == ERR_SERVICE_MARKED_FOR_DELETE:
        return ("the service is marked for deletion -- close the Services "
                "window and any open handle to it, then try again")
    return out.strip()[:300] or f"sc.exe returned {rc}"


def installed() -> bool:
    rc, out = _sc("query", SERVICE_NAME)
    return rc == 0 and "SERVICE_NAME" in out


def state() -> str:
    """-> 'absent' | 'stopped' | 'starting' | 'running' | 'stopping' | 'paused'"""
    rc, out = _sc("query", SERVICE_NAME)
    if rc != 0 or "SERVICE_NAME" not in out:
        return "absent"
    for line in out.splitlines():
        if "STATE" in line:
            for key, val in STATES.items():
                if key in line:
                    return val
    return "stopped"


def start() -> tuple[bool, str]:
    rc, out = _sc("start", SERVICE_NAME)
    if rc in (0, ERR_SERVICE_ALREADY_RUNNING) or "already been started" in out:
        return True, "service starting"
    return False, _explain(rc, out)


def stop() -> tuple[bool, str]:
    rc, out = _sc("stop", SERVICE_NAME)
    if rc in (0, ERR_SERVICE_NOT_ACTIVE) or "not been started" in out:
        return True, "service stopping"
    return False, _explain(rc, out)


def install() -> tuple[bool, str]:
    exe = service_exe()
    if exe is None:
        return False, ("VCamSimSvc.exe not found next to the app. "
                       "Install VCamSim with the installer to get the service.")
    rc, out = _sc("create", SERVICE_NAME, f"binPath= \"{exe}\"",
                  "start= auto", f"DisplayName= {SERVICE_DISPLAY}")
    if rc not in (0, ERR_SERVICE_EXISTS) and "already exists" not in out:
        return False, _explain(rc, out)
    _sc("description", SERVICE_NAME,
        "Serves the configured virtual ONVIF/RTSP cameras.")
    # restart twice on failure, then give up -- a wedged camera should not spin
    _sc("failure", SERVICE_NAME, "reset= 86400",
        "actions= restart/5000/restart/10000//0")
    return True, "service installed"


def remove() -> tuple[bool, str]:
    stop()
    # give the SCM a moment to actually stop it; deleting a running service
    # only marks it for deletion and blocks a re-install until reboot
    import time
    for _ in range(20):
        if state() in ("stopped", "absent"):
            break
        time.sleep(0.5)
    rc, out = _sc("delete", SERVICE_NAME)
    if rc in (0, ERR_SERVICE_DOES_NOT_EXIST) or "does not exist" in out:
        return True, "service removed"
    return False, _explain(rc, out)
