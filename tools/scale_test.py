"""Scale + H.265 test.

  python tools/scale_test.py [count] [concurrent_clients] [codec]
  e.g. python tools/scale_test.py 30 10 H264
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim.config import AddressingCfg, AppCfg, CameraCfg   # noqa: E402
from vcamsim.engine import Engine                              # noqa: E402
from tools.smoke_test import make_test_video                   # noqa: E402


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    clients = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    codec = sys.argv[3] if len(sys.argv) > 3 else "H264"
    src = make_test_video(ROOT / "media_cache" / "_testsrc.mp4")

    cams = []
    for i in range(1, count + 1):
        c = CameraCfg(name=f"ScaleCam {i:03d}", video=str(src))
        c.ensure_profiles()
        c.profiles[0].width, c.profiles[0].height = 1280, 720
        c.profiles[0].bitrate = 2048
        c.profiles[1].width, c.profiles[1].height = 640, 360
        for p in c.profiles:
            p.codec = codec
        cams.append(c)

    cfg = AppCfg(media_dir=str(ROOT / "media_cache"),
                 discovery_enabled=False,
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=19000, rtsp_base_port=19600),
                 cameras=cams)

    eng = Engine(cfg, log_cb=lambda l: None)
    t = time.time()
    eng.start()
    for _ in range(240):
        if len(eng.running_cameras()) == count:
            break
        time.sleep(0.5)
    print(f"{len(eng.running_cameras())}/{count} cameras up in {time.time() - t:.1f}s "
          f"({codec})")

    procs = []
    try:
        for c in eng.running_cameras()[:clients]:
            u, p = c.cfg.username, c.cfg.password
            url = c.rtsp_url(c.cfg.profiles[0]).replace("rtsp://", f"rtsp://{u}:{p}@")
            procs.append(subprocess.Popen(
                ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", url,
                 "-t", "20", "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        print(f"pulling {len(procs)} concurrent streams for 20s ...")
        time.sleep(8)
        print("active stream clients:", sum(s["clients"] for s in eng.status()))
        errs = 0
        for pr in procs:
            _, e = pr.communicate(timeout=90)
            if pr.returncode != 0 or e.strip():
                errs += 1
                print("  client error:", e.decode()[:200])
        print(f"clients finished, {errs} with errors")

        # single-stream decode sanity for the chosen codec
        c = eng.running_cameras()[0]
        url = c.rtsp_url(c.cfg.profiles[0]).replace(
            "rtsp://", f"rtsp://{c.cfg.username}:{c.cfg.password}@")
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                            "-print_format", "json", "-show_streams",
                            "-read_intervals", "%+#50", url],
                           capture_output=True, timeout=60)
        s = (json.loads(r.stdout.decode() or "{}").get("streams") or [{}])[0]
        print(f"decoded: {s.get('codec_name')} {s.get('width')}x{s.get('height')} "
              f"profile={s.get('profile')}")
    finally:
        for pr in procs:
            if pr.poll() is None:
                pr.kill()
        eng.stop()


if __name__ == "__main__":
    main()
