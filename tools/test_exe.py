"""Verify the built exe really works, in an isolated folder.

Copies dist/VCamSim.exe + ffmpeg.exe into a scratch dir with its own
config.yaml, launches it with --autostart, then exercises ONVIF, RTSP and the
snapshot over the network exactly as a VMS would.

  python tools/test_exe.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim.config import AddressingCfg, AppCfg, CameraCfg          # noqa: E402
from tools.smoke_test import (check, make_test_video, soap_call,      # noqa: E402
                              soap_call_httpdigest, OK, FAIL)

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "exe_test"

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def kill_tree(proc):
    """A one-file PyInstaller exe is a bootloader that launches a *child*.

    terminate() only kills the bootloader; the real app keeps running and keeps
    the media cache memory-mapped, so the next run cannot clean its scratch
    folder. Kill the whole tree."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()



ONVIF_PORT, RTSP_PORT = 17300, 17400


def wait_port(port: int, timeout: float = 180) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.5)
        finally:
            s.close()
    return False


def main():
    exe = ROOT / "dist" / "VCamSim.exe"
    if not exe.exists():
        print("build it first: pyinstaller --noconfirm --clean VCamSim.spec")
        sys.exit(1)

    src = make_test_video(ROOT / "media_cache" / "_testsrc.mp4")

    # rmtree(ignore_errors=True) quietly leaves the folder behind when Windows
    # still holds a handle on the copied exe, and the bare mkdir then blew up.
    scratch = SCRATCH
    for attempt in range(3):
        shutil.rmtree(scratch, ignore_errors=True)
        if not scratch.exists():
            break
        time.sleep(1.0)
    else:
        scratch = SCRATCH.with_name(f"{SCRATCH.name}_{os.getpid()}")
        print(f"note: {SCRATCH.name} is locked, using {scratch.name}")
    globals()["SCRATCH"] = scratch
    scratch.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, SCRATCH / "VCamSim.exe")
    video = SCRATCH / "clip.mp4"
    shutil.copy2(src, video)

    # ffmpeg beside the exe -- this also proves the portable layout resolves
    ff = shutil.which("ffmpeg")
    if ff:
        shutil.copy2(ff, SCRATCH / "ffmpeg.exe")
        print(f"copied ffmpeg ({(SCRATCH / 'ffmpeg.exe').stat().st_size / 1e6:.0f} MB)")

    cam = CameraCfg(name="ExeCam 01", video=str(video))
    cam.ensure_profiles()
    for p in cam.profiles:
        p.width, p.height, p.bitrate = (640, 360, 500)
        p.fps = 25
    cfg = AppCfg(media_dir="media_cache", discovery_enabled=False,
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=ONVIF_PORT,
                                          rtsp_base_port=RTSP_PORT),
                 cameras=[cam])
    cfg.save(SCRATCH / "config.yaml")

    print(f"launching {SCRATCH / 'VCamSim.exe'} --autostart ...")
    # Force the exe's own in-process engine: on a machine where the Windows
    # service is installed it would otherwise attach to the service and never
    # bind the ports this test checks.
    env = dict(os.environ, VCAMSIM_NO_SERVICE="1")
    proc = subprocess.Popen([str(SCRATCH / "VCamSim.exe"), "--autostart"],
                            cwd=str(Path.home()),     # deliberately wrong CWD
                            env=env)
    try:
        up = wait_port(ONVIF_PORT)
        check("exe starts and listens on the ONVIF port", up)
        if not up:
            return
        check("exe listens on the RTSP port", wait_port(RTSP_PORT, 30))

        base = f"http://127.0.0.1:{ONVIF_PORT}"
        dev, med = f"{base}/onvif/device_service", f"{base}/onvif/media_service"

        r = soap_call(dev, '<GetSystemDateAndTime '
                           'xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        check("exe: GetSystemDateAndTime", "UTCDateTime" in r)

        r = soap_call(dev, '<GetDeviceInformation '
                           'xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
                      "admin", "admin")
        check("exe: GetDeviceInformation (WS-Security)", "VCS-2000" in r)

        r = soap_call_httpdigest(
            dev, '<GetDeviceInformation '
                 'xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
            "admin", "admin")
        check("exe: GetDeviceInformation (HTTP Digest)", "VCS-2000" in r)

        r = soap_call(med, '<GetProfiles '
                           'xmlns="http://www.onvif.org/ver10/media/wsdl"/>',
                      "admin", "admin")
        check("exe: two media profiles", r.count("<trt:Profiles") == 2)

        url = f"rtsp://admin:admin@127.0.0.1:{RTSP_PORT}/Streaming/Channels/101"
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                            "-print_format", "json", "-show_streams",
                            "-read_intervals", "%+#40", url],
                           capture_output=True, timeout=90)
        try:
            st = (json.loads(r.stdout.decode() or "{}").get("streams") or [{}])[0]
        except json.JSONDecodeError:
            st = {}
        check("exe: RTSP main stream decodes",
              st.get("codec_name") == "h264",
              f"{st.get('codec_name')} {st.get('width')}x{st.get('height')}")

        check("exe wrote its cache next to itself, not the CWD",
              (SCRATCH / "media_cache").is_dir())
    finally:
        kill_tree(proc)

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
