"""Verify the frozen service binary, VCamSimSvc.exe.

Runs it in a scratch folder in pywin32's `debug` mode -- the same SvcDoRun the
Service Control Manager calls, but in a console -- so the whole frozen service
path is covered without Administrator or a registered service.

  python tools/test_service_exe.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim.config import AddressingCfg, AppCfg, CameraCfg    # noqa: E402
from vcamsim.control import ControlClient                      # noqa: E402
from tools.smoke_test import check, make_test_video, OK, FAIL   # noqa: E402
from tools.test_exe import kill_tree                            # noqa: E402

SCRATCH = ROOT / "build" / "svc_exe_test"
PORT = 19811
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0



def main():
    exe = ROOT / "dist" / "VCamSimSvc.exe"
    if not exe.exists():
        print("build it first: pyinstaller --noconfirm --clean VCamSimSvc.spec")
        sys.exit(1)

    src = make_test_video(ROOT / "media_cache" / "_testsrc.mp4")

    scratch = SCRATCH
    for _ in range(3):
        shutil.rmtree(scratch, ignore_errors=True)
        if not scratch.exists():
            break
        time.sleep(1.0)
    else:
        scratch = SCRATCH.with_name(f"{SCRATCH.name}_{os.getpid()}")
    scratch.mkdir(parents=True, exist_ok=True)

    shutil.copy2(exe, scratch / "VCamSimSvc.exe")
    shutil.copy2(src, scratch / "clip.mp4")
    ff = shutil.which("ffmpeg")
    if ff:
        shutil.copy2(ff, scratch / "ffmpeg.exe")
    fp = shutil.which("ffprobe")
    if fp:
        shutil.copy2(fp, scratch / "ffprobe.exe")

    cam = CameraCfg(name="SvcExeCam", video="clip.mp4")
    cam.ensure_profiles()
    for p in cam.profiles:
        p.width, p.height, p.bitrate = 640, 360, 400
    cam.import_seconds = 4
    cfg = AppCfg(discovery_enabled=False, control_port=PORT,
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=19800,
                                          rtsp_base_port=19805),
                 cameras=[cam])
    cfg.save(scratch / "config.yaml")

    # the config gives a *relative* video path: it must resolve against the
    # exe's folder, the same way media_cache does
    print(f"launching {scratch / 'VCamSimSvc.exe'} debug ...")
    proc = subprocess.Popen([str(scratch / "VCamSimSvc.exe"), "debug"],
                            cwd=str(ROOT),          # deliberately NOT scratch
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        cli = ControlClient(PORT)
        # the token is written next to the service exe, not next to this script
        deadline = time.time() + 60
        tok = ""
        while time.time() < deadline and not tok:
            try:
                tok = (scratch / "control.token").read_text(encoding="utf-8").strip()
            except OSError:
                time.sleep(0.5)
        check("service exe wrote its control token next to itself", bool(tok))
        cli.token = tok

        check("service exe answers the control channel", cli.wait_alive(60))

        deadline = time.time() + 180
        st = {}
        while time.time() < deadline:
            st = cli.status()
            if st.get("phase") == "running":
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        check("service exe brought the camera up",
              st.get("running") == 1 and st.get("phase") == "running",
              f"{st.get('phase')} {st.get('running')}/{st.get('total')}")

        cams = st.get("cameras", [])
        check("relative paths resolved against the exe, not the CWD",
              (scratch / "media_cache").is_dir())
        check("camera advertises its endpoints",
              bool(cams) and cams[0]["base_url"].startswith("http://"),
              cams[0].get("base_url", "") if cams else "")

        jpg = cli.snapshot("SvcExeCam")
        check("snapshot over the control channel",
              jpg[:2] == b"\xff\xd8", f"{len(jpg)} bytes")

        # the stream the service serves must actually decode
        url = cams[0]["urls"].get("profile_1", "")
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
             "-print_format", "json", "-show_streams", "-read_intervals", "%+#30",
             url], capture_output=True, timeout=90)
        import json
        try:
            s = (json.loads(r.stdout.decode() or "{}").get("streams") or [{}])[0]
        except json.JSONDecodeError:
            s = {}
        check("RTSP from the service exe decodes",
              s.get("codec_name") == "h264",
              f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}")

        check("remote stop works against the exe", cli.engine("stop"))
        deadline = time.time() + 30
        while time.time() < deadline and cli.status().get("running_engine"):
            time.sleep(0.3)
        check("engine stopped inside the service exe",
              not cli.status().get("running_engine"))
    finally:
        kill_tree(proc)

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
