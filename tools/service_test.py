"""Control-channel test: drives the headless runner exactly as the GUI does.

Runs the same `Runner` the Windows service hosts, but in-process and in the
foreground, so the whole service path is covered without Administrator or an
installed service.

  python tools/service_test.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim import control                                    # noqa: E402
from vcamsim.config import AddressingCfg, AppCfg, CameraCfg    # noqa: E402
from vcamsim.control import ControlClient, ControlError        # noqa: E402
from vcamsim.daemon import Runner                              # noqa: E402
from tools.smoke_test import check, make_test_video, OK, FAIL  # noqa: E402

PORT = 19755


def build_config(path: Path) -> str:
    src = make_test_video(ROOT / "media_cache" / "_testsrc.mp4")
    cams = []
    for i in (1, 2):
        c = CameraCfg(name=f"SvcCam {i:02d}", video=str(src))
        c.ensure_profiles()
        for p in c.profiles:
            p.width, p.height, p.bitrate = 640, 360, 400
        c.import_seconds = 4
        cams.append(c)
    cfg = AppCfg(media_dir=str(ROOT / "media_cache"),
                 discovery_enabled=False,
                 control_port=PORT,
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=19700,
                                          rtsp_base_port=19730),
                 cameras=cams)
    p = path / "service_test.yaml"
    cfg.save(p)
    return str(p)


def gui_proxy_checks(cfgpath: str):
    """The RemoteEngine the GUI uses must present the same surface as a local
    Engine -- that equivalence is what keeps gui.py mode-agnostic."""
    from vcamsim.config import AppCfg
    from vcamsim.proxy import LocalEngine, RemoteEngine

    cfg = AppCfg.load(cfgpath)
    rem = RemoteEngine(cfg, PORT)
    # the config replace above restarted the engine; wait for it to settle
    deadline = time.time() + 120
    while time.time() < deadline:
        if rem.refresh() and rem.stats().get("phase") == "running":
            break
        time.sleep(0.5)
    check("proxy reaches the service", rem.refresh())
    check("proxy reports the engine running", rem.running)

    surface = ["running", "start", "stop", "status", "stats", "drain_logs",
               "take_dirty", "camera", "trigger", "push_faults", "push_config",
               "mode"]
    missing = [a for a in surface
               if not hasattr(rem, a) or not hasattr(LocalEngine, a)]
    check("local and remote engines expose the same surface", not missing,
          str(missing))

    st = rem.status()
    check("proxy status has the columns the table renders",
          st and all(k in st[0] for k in
                     ("name", "ip", "onvif", "rtsp", "state", "clients",
                      "subs", "faults", "error")),
          str(list(st[0].keys()) if st else []))
    stats = rem.stats()
    check("proxy stats has the keys the tiles read",
          all(k in stats for k in ("running", "total", "clients", "subs",
                                   "faults", "mbps", "phase", "progress")),
          str(sorted(stats.keys())))

    name = st[0]["name"]
    rt = rem.camera(name)
    check("proxy hands back a camera stand-in", rt is not None)
    check("stand-in exposes the device URL",
          rt.base_url.startswith("http://"), rt.base_url)
    check("stand-in resolves a per-profile RTSP URL",
          rt.rtsp_url(rt.cfg.profiles[0]).startswith("rtsp://"),
          rt.rtsp_url(rt.cfg.profiles[0]))
    check("stand-in yields a preview frame",
          rt.snapshot()[:2] == b"\xff\xd8")
    check("unknown camera yields None", rem.camera("nope") is None)

    rt.cfg.faults.freeze_video = True
    check("proxy pushes faults to the service",
          rem.push_faults(name, rt.cfg.faults))
    rt.cfg.faults.freeze_video = False
    rem.push_faults(name, rt.cfg.faults)


def gui_attach_checks(cfgpath: str):
    """Drive the real MainWindow against the running service.

    This is the wiring that nothing else covers: the window has to render the
    service's cameras, preview through the control channel, and hand config
    changes back to the service instead of writing the file underneath it."""
    if os.environ.get("VCAMSIM_SKIP_GUI"):
        print("  SKIP  GUI attach checks (VCAMSIM_SKIP_GUI set)")
        return
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("  SKIP  GUI attach checks (PySide6 not installed)")
        return
    from vcamsim.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    from tools.qt_test_support import load_test_fonts
    load_test_fonts()

    def settle(ms=400):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    w = MainWindow(cfgpath)
    w.resize(1400, 860)
    w.show()
    settle(300)

    w.cfg.control_port = PORT
    w._attach_service()
    settle(400)
    check("GUI attaches to the service",
          w.engine is not None and w.engine.mode == "service",
          getattr(w.engine, "mode", "none"))

    deadline = time.time() + 60
    while time.time() < deadline:
        w.tick()
        settle(200)
        if w.table.rowCount() and w.table.item(0, 4).text() == "running":
            break
    check("GUI table renders the service's cameras",
          w.table.rowCount() == 2 and w.table.item(0, 4).text() == "running",
          f"{w.table.rowCount()} rows, state={w.table.item(0, 4).text()}")
    check("GUI stat tiles follow the service",
          w.tiles["cameras"]._target == 2, str(w.tiles["cameras"]._target))

    w.table.selectRow(0)
    settle(200)
    check("GUI endpoints come from the service",
          len(w.detail._urls) == 3
          and w.detail._urls[1][1].startswith("rtsp://"),
          str([u for _, u in w.detail._urls][:2]))
    w._tick_preview()
    settle(300)
    px = w.detail.preview.pixmap()
    check("GUI preview streams over the control channel",
          px is not None and not px.isNull())

    # Save must reach the service, not overwrite the file behind its back
    before = Path(cfgpath).read_text(encoding="utf-8")
    w.cfg.cameras[0].manufacturer = "PushedFromGui"
    w.save()
    settle(300)
    deadline = time.time() + 60
    while time.time() < deadline:
        if "PushedFromGui" in Path(cfgpath).read_text(encoding="utf-8"):
            break
        time.sleep(0.4)
    check("GUI Save hands the config to the service",
          "PushedFromGui" in Path(cfgpath).read_text(encoding="utf-8")
          and before != Path(cfgpath).read_text(encoding="utf-8"))

    # Start must carry the current edits over. The service starts from the
    # config *it* holds, so without a push first, Start silently re-runs the
    # old one and a corrected video path never takes effect.
    cli = ControlClient(PORT)
    cli.engine("stop")
    deadline = time.time() + 30
    while time.time() < deadline and cli.status().get("running_engine"):
        time.sleep(0.3)
    w.tick()
    settle(300)
    w.cfg.cameras[0].manufacturer = "EditedThenStarted"
    w.start()
    settle(500)
    deadline = time.time() + 120
    while time.time() < deadline:
        if "EditedThenStarted" in Path(cfgpath).read_text(encoding="utf-8"):
            break
        time.sleep(0.4)
    check("Start carries unsaved edits to the service",
          "EditedThenStarted" in Path(cfgpath).read_text(encoding="utf-8"))
    deadline = time.time() + 120
    while time.time() < deadline:
        if cli.status().get("phase") == "running":
            break
        time.sleep(0.5)
    check("the service came up after Start",
          cli.status().get("running") == 2, str(cli.status().get("running")))

    # closing the GUI must never stop the service
    w.close()
    settle(300)
    check("closing the GUI leaves the service running", cli.alive())


def main():
    out = ROOT / "build"
    out.mkdir(parents=True, exist_ok=True)
    cfgpath = build_config(out)

    runner = Runner(cfgpath, log_cb=lambda l: None)
    th = threading.Thread(target=runner.run, kwargs={"autostart": True},
                          daemon=True, name="svc-test")
    th.start()
    check("runner comes up", runner.wait_ready(20))

    cli = ControlClient(PORT)
    try:
        check("control channel answers /ping", cli.wait_alive(20))

        # ---- the token actually gates access
        bad = ControlClient(PORT)
        bad.token = "not-the-token"
        try:
            bad.status()
            gated = False
        except ControlError:
            gated = True
        check("a wrong token is rejected", gated)

        # a request with no token header at all must also be refused
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=3)
            open_access = True
        except Exception:
            open_access = False
        check("an unauthenticated request is refused", not open_access)

        # ---- wait for the engine to come up through the service
        deadline = time.time() + 120
        st = {}
        while time.time() < deadline:
            st = cli.status()
            if st.get("phase") == "running":
                break
            time.sleep(0.5)
        check("service brought both cameras up",
              st.get("running") == 2 and st.get("phase") == "running",
              f"{st.get('phase')} {st.get('running')}/{st.get('total')}")

        cams = st.get("cameras", [])
        check("status carries per-camera endpoints",
              len(cams) == 2 and cams[0]["base_url"].startswith("http://")
              and len(cams[0]["urls"]) == 2,
              json.dumps(cams[0].get("urls", {}))[:80])

        # ---- logs stream over the channel
        lines = cli.logs()
        check("logs come over the control channel", any("listening" in l for l in lines),
              f"{len(lines)} lines")

        # ---- faults apply live, to the *running* camera object
        name = cams[0]["name"]
        check("set_faults is accepted",
              cli.set_faults(name, {"offline": True, "drop_rtp_pct": 12.5}))
        rt = runner.engine.camera(name)
        check("fault reached the live camera",
              rt.cfg.faults.offline is True and rt.cfg.faults.drop_rtp_pct == 12.5,
              f"offline={rt.cfg.faults.offline} drop={rt.cfg.faults.drop_rtp_pct}")
        deadline = time.time() + 10
        while time.time() < deadline:
            if cli.status()["faults"] >= 1:
                break
            time.sleep(0.3)
        check("status reports the faulted camera", cli.status()["faults"] >= 1)
        cli.set_faults(name, {"offline": False, "drop_rtp_pct": 0.0})
        check("faults clear again", not rt.cfg.faults.any_active())

        # ---- manual event trigger
        check("trigger is accepted", cli.trigger(name, "motion", True))
        time.sleep(0.4)
        cli.trigger(name, "motion", False)

        # ---- snapshot over the channel (this is what the GUI preview uses)
        jpg = cli.snapshot(name)
        check("snapshot comes over the control channel",
              jpg[:2] == b"\xff\xd8" and len(jpg) > 1000, f"{len(jpg)} bytes")
        check("an unknown camera yields no snapshot", cli.snapshot("nope") == b"")

        # ---- stop / start the engine remotely
        check("remote stop", cli.engine("stop"))
        deadline = time.time() + 30
        while time.time() < deadline and cli.status().get("running_engine"):
            time.sleep(0.3)
        check("engine reports stopped", not cli.status().get("running_engine"))
        check("remote start", cli.engine("start"))
        deadline = time.time() + 120
        while time.time() < deadline:
            if cli.status().get("phase") == "running":
                break
            time.sleep(0.5)
        check("engine came back up", cli.status().get("running") == 2,
              str(cli.status().get("running")))

        # ---- config round trip
        cfg = cli.get_config()
        check("config is readable", len(cfg.get("cameras", [])) == 2)
        cfg["cameras"][0]["name"] = "Renamed Cam"
        check("config is writable", cli.put_config(cfg))
        deadline = time.time() + 120
        while time.time() < deadline:
            names = [c["name"] for c in cli.status().get("cameras", [])]
            if "Renamed Cam" in names:
                break
            time.sleep(0.5)
        check("the new config took effect",
              "Renamed Cam" in [c["name"] for c in cli.status().get("cameras", [])],
              str([c["name"] for c in cli.status().get("cameras", [])]))
        check("the new config was persisted",
              "Renamed Cam" in Path(cfgpath).read_text(encoding="utf-8"))
        # ---- the GUI proxy drives the same runner
        gui_proxy_checks(cfgpath)
        gui_attach_checks(cfgpath)
    finally:
        runner.stop()
        th.join(timeout=30)
        check("runner shut down", not th.is_alive())

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
