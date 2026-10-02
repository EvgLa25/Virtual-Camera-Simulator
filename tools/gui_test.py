"""UI test: build every window and dialog, exercise it, save screenshots.

The point is to catch paint and layout errors that a plain import check cannot,
and to leave a set of PNGs that show what the UI actually looks like.

Windows keeps the native platform plugin (Qt no longer ships the fonts the
offscreen plugin needs); elsewhere it runs fully headless.

  python tools/gui_test.py [outdir]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# This test drives the GUI's own in-process engine. On a machine where the
# Windows service is installed the window would otherwise attach to it and
# adopt the service's config instead of the fixture's.
os.environ["VCAMSIM_NO_SERVICE"] = "1"

from PySide6.QtCore import QTimer                      # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget    # noqa: E402

from vcamsim import widgets                            # noqa: E402
from vcamsim.config import AppCfg, CameraCfg           # noqa: E402
from vcamsim.gui import (                              # noqa: E402
    CameraDialog, FaultDialog, GenerateDialog, MainWindow, SettingsDialog,
)

OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))


def settle(app, ms: int = 320):
    """Spin the event loop so entry animations (dialog fades) finish."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def copy_via(app, fn, tries: int = 40) -> str:
    """Run `fn` (which copies) and return what it put on the clipboard.

    Seeds a sentinel and waits for it to change rather than clearing first:
    two setText calls in quick succession can land out of order on Windows,
    and the reader then sees the empty string."""
    cb = QApplication.clipboard()
    sentinel = "<<vcamsim-test-sentinel>>"
    cb.setText(sentinel)
    settle(app, 60)
    fn()
    for _ in range(tries):
        settle(app, 40)
        txt = cb.text()
        if txt and txt != sentinel:
            return txt
    return ""


def distinct_colors(px, step: int = 17) -> int:
    """Rough colour count -- a widget captured mid-fade grabs as one flat colour."""
    img = px.toImage()
    seen = set()
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            seen.add(img.pixel(x, y))
            if len(seen) > 8:
                return len(seen)
    return len(seen)


def shot(w, out: Path, name: str):
    settle(QApplication.instance(), 260)      # let any entry animation finish
    px = w.grab()
    n = distinct_colors(px) if not px.isNull() else 0
    ok = not px.isNull() and px.width() > 10 and n > 3
    if not px.isNull():
        px.save(str(out / f"{name}.png"))
    check(f"render {name}", ok, f"{px.width()}x{px.height()}, {n} colours")
    return ok


def sample_config(path: Path) -> str:
    cfg = AppCfg()
    states = ["running", "running", "stopped", "error", "running"]
    for i, st in enumerate(states, 1):
        c = CameraCfg(name=f"VCam {i:02d} & Co", video=str(ROOT / "media_cache" / "_testsrc.mp4"))
        c.ensure_profiles()
        c.enabled = st != "disabled"
        if i == 2:
            c.faults.freeze_video = True
            c.faults.drop_rtp_pct = 12.5
        cfg.cameras.append(c)
    p = path / "gui_test.yaml"
    cfg.save(p)
    return str(p)


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "uishots"
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    from tools.qt_test_support import load_test_fonts
    load_test_fonts()
    cfgpath = sample_config(out)

    w = MainWindow(cfgpath)
    w.resize(1420, 880)
    w.show()
    app.processEvents()
    check("main window built", w.table.rowCount() == 5, f"{w.table.rowCount()} rows")

    # fake some live state so the pills, badges and tiles all have something
    for r, st in enumerate(["running", "running", "stopped", "error", "running"]):
        w.table.item(r, 4).setText(st)
        w.table.item(r, 1).setText("10.10.1.5")
        w.table.item(r, 2).setText(str(8000 + r))
        w.table.item(r, 3).setText(str(554 + r))
        w.table.item(r, 5).setText(str(r % 3))
        w.table.item(r, 6).setText(str(r % 2))
    w.table.item(1, 7).setText("FAULT")
    w.table.item(3, 8).setText("[Errno 10048] address already in use")
    w.table.selectRow(1)
    w._update_stats({"running": 3, "total": 5, "clients": 4, "subs": 2,
                     "faults": 1, "mbps": 8.42, "phase": "running",
                     "progress": (5, 5), "error": 1})
    for v in (2.1, 5.5, 9.2, 7.4, 8.9, 12.0, 6.6, 8.4):
        w.tiles["mbps"].push_spark(v)
    for line in ["12:00:01 [engine] ffmpeg: version 8.0.1",
                 "12:00:02 [VCam 01] HTTP/ONVIF listening on 0.0.0.0:8000",
                 "12:00:02 [VCam 01] RTSP listening on 0.0.0.0:554",
                 "12:00:03 [VCam 04] start failed: address already in use",
                 "12:00:04 [engine] WARNING: no Windows Firewall rule found",
                 "12:00:05 [VCam 02] event tns1:RuleEngine/Motion {'IsMotion': 'true'}"]:
        w._log(line)
    app.processEvents()
    shot(w, out, "01_main_dark")

    # the header must not clip its labels, even at the minimum window width
    from PySide6.QtWidgets import QPushButton as _QPB
    hdr = w.findChild(type(w.centralWidget()), "Header") or w.b_start.parent()
    for width in (w.minimumWidth(), 1420):
        w.resize(width, 880)
        settle(app, 120)
        clipped = [b.text() for b in hdr.findChildren(_QPB)
                   if b.text() and b.width() < b.sizeHint().width() - 1]
        check(f"header fits at {width}px", not clipped, str(clipped))
    w.resize(1420, 880)
    settle(app, 120)

    # animations must not explode when driven
    widgets.pulse()._step()
    w.tiles["cameras"].anim.setCurrentTime(200)
    widgets.toast(w, "Engine started", "ok")
    widgets.toast(w, "Port 554 already in use", "err")
    app.processEvents()
    check("toasts stack", len(widgets.Toast._live) == 2, str(len(widgets.Toast._live)))
    shot(w, out, "02_main_toasts")

    # Qt reads a 9-digit hex as #AARRGGBB, so an "#RRGGBBAA" token comes out
    # nearly transparent -- keep every palette token plain #RRGGBB.
    import dataclasses as _dc
    from vcamsim.theme import DARK, LIGHT
    bad = [(th.name, f.name, getattr(th, f.name))
           for th in (DARK, LIGHT) for f in _dc.fields(th)
           if f.name != "name" and len(str(getattr(th, f.name))) != 7]
    check("every palette token is plain #RRGGBB", not bad, str(bad))
    check("busy overlay scrim is actually opaque",
          widgets.theme().scrim().alpha() > 180,
          str(widgets.theme().scrim().alpha()))

    w.overlay.start("Preparing media", "camera 2 of 5 - transcoding")
    w.overlay.update_text(frac=0.4)
    w.overlay.resize(w.body.size())
    app.processEvents()
    shot(w, out, "03_busy_overlay")
    w.overlay.stop()

    # log filtering
    w._set_filter("rtsp")
    body = w.log.toPlainText()
    check("log filter narrows output", "RTSP listening" in body and
          "ffmpeg" not in body, repr(body[:60]))
    w._set_filter("")
    check("clearing the filter restores every line",
          w.log.toPlainText().count("\n") >= 5)

    # theme round trip
    w.toggle_theme()
    app.processEvents()
    check("theme switched to light", widgets.theme().name == "light")
    shot(w, out, "04_main_light")
    w.toggle_theme()
    app.processEvents()
    check("theme switched back to dark", widgets.theme().name == "dark")

    # dialogs
    cam = w.cfg.cameras[0]
    d1 = CameraDialog(cam, w)
    d1.resize(760, 600)
    d1.show()
    settle(app)
    shot(d1, out, "05_camera_dialog")
    got = d1.result_cfg()
    check("camera dialog round-trips its profiles",
          len(got.profiles) == 2 and got.profiles[0].width == 1920,
          f"{len(got.profiles)} profiles")
    d1.close()

    d2 = FaultDialog(cam, None, w)
    d2.resize(560, 720)
    d2.show()
    settle(app)
    n_widgets = len(d2.widgets)
    import dataclasses as dc
    check("fault dialog exposes every FaultCfg field",
          n_widgets == len(dc.fields(cam.faults)),
          f"{n_widgets} of {len(dc.fields(cam.faults))}")
    d2.widgets["offline"].setChecked(True)
    d2.widgets["drop_rtp_pct"].setValue(25.0)
    d2.apply()
    check("toggling a fault writes through to the config",
          cam.faults.offline is True and cam.faults.drop_rtp_pct == 25.0)
    shot(d2, out, "06_fault_dialog")
    d2.clear()
    check("clear resets every fault", not cam.faults.any_active())
    d2.close()

    d3 = GenerateDialog(w)
    d3.show()
    settle(app)
    shot(d3, out, "07_generate_dialog")
    cams = d3.build()
    check("generate builds the requested count", len(cams) == 30, str(len(cams)))
    check("generated names are unique",
          len({c.name for c in cams}) == 30)
    check("generated cameras carry no stale addresses",
          all(not c.ip and not c.onvif_port for c in cams))
    d3.close()

    # bulk edit: only ticked fields change, scope narrows to the selection
    from vcamsim.gui import BulkEditDialog
    w.table.clearSelection()
    w.table.selectRow(0)
    w.table.selectionModel().select(
        w.table.model().index(2, 0),
        w.table.selectionModel().SelectionFlag.Select
        | w.table.selectionModel().SelectionFlag.Rows)
    picked = w.sel_cfgs()
    check("table multi-select feeds bulk edit", len(picked) == 2,
          str([c.name for c in picked]))
    d5 = BulkEditDialog(w.cfg.cameras, picked, w)
    d5.show()
    settle(app)
    d5.fields["bitrate"][0].setChecked(True)
    d5.bitrate.setValue(777)
    d5.fields["fps"][0].setChecked(True)
    d5.fps.setValue(12)
    d5.target.setCurrentIndex(2)                 # all streams
    shot(d5, out, "08a_bulk_edit_dialog")
    before_names = [c.name for c in w.cfg.cameras]
    n, warns = d5.apply()
    d5.close()
    check("bulk edit changes only the selected cameras",
          n == 2 and all(p.bitrate == 777 and p.fps == 12
                         for c in picked for p in c.profiles)
          and w.cfg.cameras[1].profiles[0].bitrate != 777, f"{n} changed")
    check("bulk edit leaves untouched fields alone",
          [c.name for c in w.cfg.cameras] == before_names
          and all(c.profiles[0].width == 1920 for c in picked))
    w.table.clearSelection()

    d4 = SettingsDialog(w.cfg, w)
    d4.show()
    settle(app)
    shot(d4, out, "08_settings_dialog")
    d4.mode.setCurrentText("alias")
    d4.apply()
    check("settings write back", w.cfg.addressing.mode == "alias")
    d4.close()

    # per-row action buttons
    from PySide6.QtWidgets import QPushButton
    aw = w.table.cellWidget(0, w.table.columnCount() - 1)
    btns = aw.findChildren(QPushButton) if aw else []
    check("every row carries its own action buttons", len(btns) == 4,
          f"{len(btns)} buttons on row 0")
    check("every row has an action widget",
          all(w.table.cellWidget(r, w.table.columnCount() - 1) is not None
              for r in range(w.table.rowCount())))

    def live_action_widgets():
        return [x for x in w.table.viewport().findChildren(QWidget, "RowActions")
                if x.isVisible()]

    # shrinking the list must not leave orphaned buttons painting over blank rows
    keep = w.cfg.cameras[:]
    w.cfg.cameras = keep[:1]
    w.rebuild_table()
    settle(app, 150)
    check("shrinking the list removes the old row buttons",
          len(live_action_widgets()) == 1,
          f"{len(live_action_widgets())} widgets for 1 row")
    w.cfg.cameras = keep
    w.rebuild_table()
    settle(app, 150)
    check("growing the list restores one set per row",
          len(live_action_widgets()) == len(keep),
          f"{len(live_action_widgets())} widgets for {len(keep)} rows")
    # the buttons are bound to the camera object, not a row number
    target = w.cfg.cameras[3]
    w.table.selectRow(0)
    w._select(target)
    check("row buttons act on their own camera",
          w.table.currentRow() == 3 and w._row_index(target) == 3,
          f"row {w.table.currentRow()}")
    w.cfg.cameras.insert(0, w.cfg.cameras.pop(3))       # reorder
    check("row binding survives a reorder", w._row_index(target) == 0,
          str(w._row_index(target)))
    w.cfg.cameras.insert(3, w.cfg.cameras.pop(0))
    w.rebuild_table()

    # selection + detail panel
    w.table.selectRow(0)
    app.processEvents()
    check("detail panel follows the selection",
          w.detail.cam_name == w.cfg.cameras[0].name, w.detail.cam_name)
    check("detail panel shows the serial",
          w.detail.rows["Serial"].text() not in ("", "-") or not w.cfg.cameras[0].serial)

    # ---- live engine through the UI (the async start path)
    src = ROOT / "media_cache" / "_testsrc.mp4"
    if src.exists():
        live = CameraCfg(name="LiveCam", video=str(src))
        live.ensure_profiles()
        for p in live.profiles:
            p.width, p.height, p.bitrate = 640, 360, 400
        w.cfg.cameras = [live]
        w.cfg.addressing.mode = "port"
        w.cfg.addressing.advertise_ip = "127.0.0.1"
        w.cfg.addressing.onvif_base_port = 18400
        w.cfg.addressing.rtsp_base_port = 18450
        w.cfg.discovery_enabled = False
        w.rebuild_table()
        w.start()
        check("start returns immediately (no UI freeze)", w.engine is not None)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            settle(app, 200)
            if w.engine and w.engine.stats()["phase"] == "running":
                break
        settle(app, 600)          # let the UI tick pick the new phase up
        st = w.engine.stats() if w.engine else {}
        check("engine reaches running through the UI",
              st.get("running") == 1 and st.get("phase") == "running",
              f"{st.get('phase')} {st.get('running')}/{st.get('total')}")
        check("busy overlay closed itself", not w.overlay.isVisible())
        check("table shows the running state",
              w.table.item(0, 4).text() == "running", w.table.item(0, 4).text())
        w.table.selectRow(0)
        settle(app, 200)
        eps = w.detail._urls
        check("detail panel lists device + one row per stream",
              len(eps) == 1 + len(live.profiles),
              f"{len(eps)} rows: {[l for l, _ in eps]}")
        check("stream endpoints are live rtsp URLs",
              all(u.startswith("rtsp://") for _, u in eps[1:]),
              str([u[:28] for _, u in eps[1:]]))

        # per-stream copy: each row copies ONLY its own URL
        one = copy_via(app, lambda: w.detail._copy_one(1))
        check("copying one stream copies just that stream",
              one == eps[1][1] and "\n" not in one, repr(one[:44]))
        two = copy_via(app, lambda: w.detail._copy_one(2))
        check("the sub stream copies its own distinct URL",
              two == eps[2][1] and two != one, repr(two[:44]))
        allt = copy_via(app, w.detail._copy_all)
        check("copy-all still yields every endpoint",
              len(allt.splitlines()) == len(eps),
              f"{len(allt.splitlines())} lines")
        w._tick_preview()
        check("live preview produced a frame",
              w.detail.preview.pixmap() is not None
              and not w.detail.preview.pixmap().isNull())
        shot(w, out, "09_main_live")
        w.stop()
        check("stop returns immediately and disables both buttons",
              w._stopping and not w.b_stop.isEnabled() and not w.b_start.isEnabled())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and w._stopping:
            settle(app, 100)
        check("stop tears the engine down", not w._stopping and w.engine is None)
        check("table falls back to stopped",
              w.table.item(0, 4).text() == "stopped", w.table.item(0, 4).text())
    else:
        print("  SKIP  live engine test (no media_cache/_testsrc.mp4)")

    # start with no cameras must be refused, not hang
    w.cfg.cameras = []
    w.rebuild_table()
    w.start()
    check("start with no cameras is refused", w.engine is None)

    w.close()
    QTimer.singleShot(0, app.quit)
    app.exec()

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    print(f"screenshots -> {out}")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
