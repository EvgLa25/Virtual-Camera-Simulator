"""Windows service wrapper around the headless runner.

Built into its own console binary (VCamSimSvc.exe) because a pywin32 service
host cannot be a windowed exe. The GUI never becomes the service -- session 0
has no desktop -- it drives this over the loopback control channel.

    VCamSimSvc.exe install|start|stop|remove
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_NAME = "VCamSim"
SERVICE_DISPLAY = "VCamSim Virtual ONVIF Cameras"
SERVICE_DESC = ("Serves the configured virtual ONVIF/RTSP cameras. "
                "Configure it with the VCamSim desktop app.")


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> str:
    return str(app_dir() / "config.yaml")


try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    HAVE_PYWIN32 = True
except ImportError:                                   # non-Windows / no pywin32
    HAVE_PYWIN32 = False


if HAVE_PYWIN32:
    class VCamSimService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            super().__init__(args)
            self.hstop = win32event.CreateEvent(None, 0, 0, None)
            self.runner = None

        def SvcStop(self):
            # a transcode being killed and 30 cameras closing sockets can take
            # a while; tell the SCM so it does not declare us hung at 30 s
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,
                                     waitHint=45000)
            if self.runner:
                self.runner.stop()
            win32event.SetEvent(self.hstop)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
            # imported here so a missing dependency is logged as a service
            # error rather than killing the service host at import time
            from .daemon import Runner
            try:
                self.runner = Runner(config_path(), log_cb=_file_log)
                self.runner.run(autostart=True)
            except Exception as e:
                servicemanager.LogErrorMsg(f"VCamSim service failed: {e!r}")
                _file_log(f"FATAL: {e!r}")
                raise


def _file_log(line: str):
    """Append to service.log beside the exe. A service has no console, so
    without this a start-up failure leaves nothing to read but the Event Log.
    Rotated at ~5 MB by truncating the oldest half."""
    try:
        p = app_dir() / "service.log"
        try:
            if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
                keep = p.read_bytes()[-2 * 1024 * 1024:]
                p.write_bytes(keep[keep.find(b"\n") + 1:])
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv)
    if not HAVE_PYWIN32:
        print("pywin32 is required for the Windows service "
              "(pip install pywin32)")
        return 1
    if len(argv) == 1:
        # launched by the Service Control Manager
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(VCamSimService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    win32serviceutil.HandleCommandLine(VCamSimService, argv=argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
