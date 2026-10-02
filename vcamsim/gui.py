"""PySide6 desktop UI: dashboard, camera grid, fault console and live log.

The window never blocks: `Engine.start()` returns immediately and does its
transcoding on its own thread, so the overlay animates and Stop stays clickable
even during a first-run import.
"""
from __future__ import annotations

import dataclasses as dc
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction, QColor, QCursor, QFont, QGuiApplication, QKeySequence, QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLayout, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import firewall, icons, media, svcctl, widgets
from .addressing import is_admin
from .config import AppCfg, CameraCfg, ProfileCfg, from_dict, generate_cameras, to_dict
from .proxy import LocalEngine, RemoteEngine
from .theme import THEMES, stylesheet
from .widgets import (
    BusyOverlay, Card, RowDelegate, StatTile, ToggleSwitch, fade_in, toast,
)

COLS = ["Camera", "IP", "ONVIF", "RTSP", "State", "Clients", "Subs", "Faults",
        "Error", ""]
COL_KIND = [None, "mono", "mono", "mono", "state", "num", "num", "fault",
            "error", "actions"]
ACT_COL = len(COLS) - 1

FAULT_GROUPS = [
    ("Reachability", ["offline", "onvif_offline", "rtsp_offline",
                      "hide_from_discovery"]),
    ("Authentication & time", ["reject_auth", "clock_drift_sec"]),
    ("SOAP", ["soap_latency_ms", "soap_timeout", "soap_fault", "malformed_xml",
              "truncate_response"]),
    ("Streaming", ["refuse_describe", "stream_drop_after_sec", "freeze_video",
                   "corrupt_nal_pct", "drop_rtp_pct", "bitrate_collapse"]),
    ("Events", ["event_flood", "event_stall", "subscription_expire_sec"]),
]

FAULT_HELP = {
    "offline": "Drop every ONVIF and RTSP connection -- camera looks unplugged",
    "onvif_offline": "ONVIF dies, RTSP keeps streaming",
    "rtsp_offline": "RTSP dies, ONVIF stays answerable",
    "hide_from_discovery": "Stop answering WS-Discovery probes",
    "reject_auth": "Reject even correct credentials",
    "clock_drift_sec": "Shift the device clock -- breaks WS-Security digests",
    "soap_latency_ms": "Delay every SOAP reply",
    "soap_timeout": "Accept the request and never answer",
    "soap_fault": "Answer every operation with a SOAP Fault",
    "malformed_xml": "Return truncated, unparseable XML",
    "truncate_response": "Cut every response in half",
    "refuse_describe": "Answer DESCRIBE with 503",
    "stream_drop_after_sec": "Kill the RTP stream N seconds after PLAY",
    "freeze_video": "Keep sending the same frame for ever",
    "corrupt_nal_pct": "Flip bits in this %% of NAL units",
    "drop_rtp_pct": "Silently drop this %% of RTP packets",
    "bitrate_collapse": "Send keyframes only",
    "event_flood": "Fire motion events as fast as the client pulls",
    "event_stall": "Never answer PullMessages",
    "subscription_expire_sec": "Expire subscriptions after N seconds",
}

UNITS = {"clock_drift_sec": " s", "soap_latency_ms": " ms",
         "stream_drop_after_sec": " s", "subscription_expire_sec": " s",
         "corrupt_nal_pct": " %", "drop_rtp_pct": " %"}


def t():
    return widgets.theme()


def button(text: str, icon_name: str = "", kind: str = "", tip: str = "",
           on_click=None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if icon_name:
        col = "#ffffff" if kind == "primary" else t().text_dim
        b.setProperty("icon_name", icon_name)
        b.setIcon(icons.icon(icon_name, col, 16))
        b.setIconSize(QSize(16, 16))
    if tip:
        b.setToolTip(tip)
    if on_click:
        b.clicked.connect(on_click)
    b.setCursor(Qt.PointingHandCursor)
    return b


def vsep() -> QFrame:
    f = QFrame()
    f.setObjectName("Divider")
    f.setFixedWidth(1)
    f.setFixedHeight(22)
    return f


# =============================================================== dialogs
class BaseDialog(QDialog):
    """Every dialog fades in and picks up the app stylesheet."""

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_faded", False):
            self._faded = True
            fade_in(self, 160)


class ProfileTable(QTableWidget):
    HDR = ["Name", "Token", "Width", "Height", "FPS", "kbps", "Codec", "RTSP path"]

    def __init__(self, profiles):
        super().__init__(0, len(self.HDR))
        self.setHorizontalHeaderLabels(self.HDR)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.horizontalHeader().setHighlightSections(False)
        for c in range(len(self.HDR)):
            self.horizontalHeaderItem(c).setTextAlignment(
                (Qt.AlignVCenter | Qt.AlignLeft))
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(30)
        self.setMinimumHeight(120)
        for p in profiles:
            self.add(p)

    def add(self, p: ProfileCfg | None = None):
        n = self.rowCount() + 1
        p = p or ProfileCfg(f"Stream{n}", f"profile_{n}",
                            stream_path=f"/Streaming/Channels/10{n}")
        r = self.rowCount()
        self.insertRow(r)
        vals = [p.name, p.token, p.width, p.height, p.fps, p.bitrate, None,
                p.stream_path]
        for c, v in enumerate(vals):
            if c == 6:
                cb = QComboBox()
                cb.addItems(["H264", "H265"])
                cb.setCurrentText(p.codec)
                self.setCellWidget(r, c, cb)
            else:
                self.setItem(r, c, QTableWidgetItem(str(v)))

    def collect(self) -> list[ProfileCfg]:
        out = []
        seen_tok: set[str] = set()
        for r in range(self.rowCount()):
            def g(c, _r=r):
                it = self.item(_r, c)
                return it.text().strip() if it else ""

            def num(c, default, lo, hi, _r=r):
                try:
                    v = int(float(g(c, _r) or default))
                except ValueError:
                    v = default
                return min(hi, max(lo, v))
            # yuv420p needs even dimensions and ffmpeg refuses anything tiny
            w = num(2, 1920, 16, 7680)
            h = num(3, 1080, 16, 4320)
            tok = g(1) or f"profile_{r + 1}"
            base, k = tok, 2
            while tok in seen_tok:
                tok = f"{base}_{k}"
                k += 1
            seen_tok.add(tok)
            sp = g(7) or f"/Streaming/Channels/10{r + 1}"
            if not sp.startswith("/"):
                sp = "/" + sp
            out.append(ProfileCfg(
                name=g(0) or f"Stream{r + 1}", token=tok,
                width=w - w % 2, height=h - h % 2,
                fps=num(4, 25, 1, 120), bitrate=num(5, 2048, 32, 100000),
                codec=self.cellWidget(r, 6).currentText(),
                stream_path=sp))
        return out


class CameraDialog(BaseDialog):
    def __init__(self, cfg: CameraCfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Camera - {cfg.name}")
        self.resize(760, 600)
        self.cfg = from_dict(CameraCfg, to_dict(cfg))
        self.cfg.ensure_profiles()

        self.name = QLineEdit(self.cfg.name)
        self.video = QLineEdit(self.cfg.video)
        self.video.setCursorPosition(0)      # show the start of a long path
        self.video.textChanged.connect(self._probe_later)
        browse = button("Browse", "folder", on_click=self._browse)
        vrow = QHBoxLayout()
        vrow.setContentsMargins(0, 0, 0, 0)
        vrow.addWidget(self.video, 1)
        vrow.addWidget(browse)
        vw = QWidget()
        vw.setLayout(vrow)
        self.vinfo = QLabel("")
        self.vinfo.setObjectName("Subtle")
        self.vinfo.setStyleSheet("font-size:11px;")

        self.user = QLineEdit(self.cfg.username)
        self.pwd = QLineEdit(self.cfg.password)
        self.pwd.setEchoMode(QLineEdit.Password)
        reveal = self.pwd.addAction(icons.icon("eye", t().text_dim), QLineEdit.TrailingPosition)
        reveal.setToolTip("Show or hide password")
        reveal.setCheckable(True)
        reveal.toggled.connect(lambda on: self.pwd.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.manu = QLineEdit(self.cfg.manufacturer)
        self.model = QLineEdit(self.cfg.model)
        self.fw = QLineEdit(self.cfg.firmware)
        self.motion = QSpinBox()
        self.motion.setRange(0, 3600)
        self.motion.setValue(self.cfg.motion_interval_sec)
        self.motion.setSuffix(" s")
        self.motion.setSpecialValueText("off")
        self.imp = QSpinBox()
        self.imp.setRange(0, 86400)
        self.imp.setValue(self.cfg.import_seconds)
        self.imp.setSuffix(" s")
        self.imp.setSpecialValueText("whole file")
        self.imp.setToolTip(
            "Only transcode the first N seconds of the source.\n"
            "The stream loops for ever, so a long file just means a long\n"
            "first-run import. 0 imports everything.")
        self.creds = QCheckBox("GetStreamUri returns rtsp://user:pass@host/...")
        self.creds.setChecked(self.cfg.stream_uri_credentials)
        self.pin_o = QSpinBox()
        self.pin_o.setRange(0, 65000)
        self.pin_o.setValue(self.cfg.pin_onvif_port)
        self.pin_o.setSpecialValueText("auto")
        self.pin_r = QSpinBox()
        self.pin_r.setRange(0, 65000)
        self.pin_r.setValue(self.cfg.pin_rtsp_port)
        self.pin_r.setSpecialValueText("auto")
        self.enabled = QCheckBox("Camera enabled")
        self.enabled.setChecked(self.cfg.enabled)

        ident = Card("Identity")
        fi = QFormLayout()
        fi.setSpacing(9)
        for lbl, w in [("Name", self.name), ("Manufacturer", self.manu),
                       ("Model", self.model), ("Firmware", self.fw)]:
            fi.addRow(lbl, w)
        ident.box.addLayout(fi)
        ident.box.addStretch()          # pack to the top, not the middle

        src = Card("Source & access")
        fs = QFormLayout()
        fs.setSpacing(9)
        fs.addRow("Video file", vw)
        fs.addRow("", self.vinfo)
        for lbl, w in [("Import only first", self.imp),
                       ("Username", self.user), ("Password", self.pwd),
                       ("Auto motion every", self.motion),
                       ("Pin ONVIF port", self.pin_o),
                       ("Pin RTSP port", self.pin_r),
                       ("", self.creds), ("", self.enabled)]:
            fs.addRow(lbl, w)
        src.box.addLayout(fs)

        self.profiles = ProfileTable(self.cfg.profiles)
        prof = Card("Stream profiles  (first = main stream)")
        prof.box.addWidget(self.profiles)
        prow = QHBoxLayout()
        prow.addWidget(button("Add profile", "plus", on_click=lambda: self.profiles.add()))
        prow.addWidget(button("Remove", "trash", "danger",
                              on_click=self._del_profile))
        prow.addStretch()
        prof.box.addLayout(prow)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        cols.addWidget(ident, 1)
        cols.addWidget(src, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)
        lay.addLayout(cols)
        lay.addWidget(prof, 1)
        lay.addWidget(bb)

        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.timeout.connect(self._probe_now)
        self._probe_later()

    def _del_profile(self):
        r = self.profiles.currentRow()
        if r >= 0 and self.profiles.rowCount() > 1:
            self.profiles.removeRow(r)

    def _probe_later(self):
        self._probe_timer.start(450)

    def _probe_now(self):
        p = self.video.text().strip()
        if not p:
            self.vinfo.setText("")
            return
        if not Path(p).exists():
            self.vinfo.setText("file not found")
            return
        self.vinfo.setText(media.describe(p) or "unreadable by ffprobe")

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Video file", "", "Video (*.mp4 *.mkv *.avi *.mov *.ts);;All (*)")
        if p:
            self.video.setText(p)

    def result_cfg(self) -> CameraCfg:
        c = self.cfg
        c.name = self.name.text().strip() or c.name
        c.video = self.video.text().strip()
        c.username = self.user.text()
        c.password = self.pwd.text()
        c.manufacturer = self.manu.text()
        c.model = self.model.text()
        c.firmware = self.fw.text()
        c.motion_interval_sec = self.motion.value()
        c.import_seconds = self.imp.value()
        c.stream_uri_credentials = self.creds.isChecked()
        c.pin_onvif_port = self.pin_o.value()
        c.pin_rtsp_port = self.pin_r.value()
        c.enabled = self.enabled.isChecked()
        c.profiles = self.profiles.collect()
        c.uuid = c.mac = c.serial = ""     # re-derive from the (possibly new) name
        return c


class FaultDialog(BaseDialog):
    """Grouped fault console. Every change applies to the live camera at once."""

    def __init__(self, cam_cfg: CameraCfg, engine, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Fault injection - {cam_cfg.name}")
        self.resize(560, 720)
        self.cfg = cam_cfg
        self.engine = engine
        self.widgets: dict[str, QWidget] = {}
        # config.py uses `from __future__ import annotations`, so dc.fields()
        # reports the annotation as a *string*; switch on the live value.
        known = {f.name for f in dc.fields(cam_cfg.faults)}
        missing = [n for n in known
                   if n not in {x for _, g in FAULT_GROUPS for x in g}]

        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(2, 2, 2, 2)
        col.setSpacing(12)
        groups = FAULT_GROUPS + ([("Other", missing)] if missing else [])
        for title, names in groups:
            card = Card(title)
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(9)
            grid.setColumnStretch(0, 1)
            for row, nm in enumerate(names):
                val = getattr(cam_cfg.faults, nm)
                lbl = QLabel(nm.replace("_", " "))
                lbl.setToolTip(FAULT_HELP.get(nm, ""))
                sub = QLabel(FAULT_HELP.get(nm, ""))
                sub.setObjectName("Subtle")
                sub.setStyleSheet("font-size:10px;")
                sub.setWordWrap(True)
                cell = QVBoxLayout()
                cell.setSpacing(0)
                cell.addWidget(lbl)
                cell.addWidget(sub)
                holder = QWidget()
                holder.setLayout(cell)
                grid.addWidget(holder, row, 0)

                if isinstance(val, bool):
                    w = ToggleSwitch()
                    w.setChecked(bool(val))
                    w.toggled.connect(self.apply)
                elif isinstance(val, float):
                    w = QDoubleSpinBox()
                    w.setRange(0, 100)
                    w.setDecimals(1)
                    w.setSingleStep(1.0)
                    w.setValue(float(val))
                    w.setSuffix(UNITS.get(nm, ""))
                    w.valueChanged.connect(self.apply)
                else:
                    w = QSpinBox()
                    w.setRange(-86400, 86400)
                    w.setValue(int(val))
                    w.setSuffix(UNITS.get(nm, ""))
                    if nm != "clock_drift_sec":
                        w.setMinimum(0)
                        w.setSpecialValueText("off")
                    w.valueChanged.connect(self.apply)
                w.setToolTip(FAULT_HELP.get(nm, ""))
                if not isinstance(w, ToggleSwitch):
                    w.setFixedWidth(118)     # line the numbers up with the switches
                self.widgets[nm] = w
                grid.addWidget(w, row, 1, Qt.AlignRight)
            card.box.addLayout(grid)
            col.addWidget(card)
        col.addStretch()

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(inner)

        ev = Card("Manual triggers")
        evl = QHBoxLayout()
        evl.setSpacing(8)
        for txt, ic, fn in [
                ("Motion on", "waves", lambda: self._fire("motion", True)),
                ("Motion off", "waves", lambda: self._fire("motion", False)),
                ("Tamper", "alert", lambda: self._fire("tamper", True)),
                ("Signal loss", "x", lambda: self._fire("signal_loss", True))]:
            evl.addWidget(button(txt, ic, on_click=fn))
        evl.addStretch()
        ev.box.addLayout(evl)

        foot = QHBoxLayout()
        foot.addWidget(button("Clear all faults", "refresh", "danger",
                              on_click=self.clear))
        foot.addStretch()
        close = button("Close", kind="primary", on_click=self.accept)
        foot.addWidget(close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        hint = QLabel("Changes apply immediately to the running camera.")
        hint.setObjectName("Subtle")
        lay.addWidget(hint)
        lay.addWidget(sc, 1)
        lay.addWidget(ev)
        lay.addLayout(foot)

    def _fire(self, kind: str, on: bool):
        if not self.engine:
            toast(self, "Engine is not running", "warn")
            return
        self.engine.trigger(self.cfg.name, kind, on)

    def apply(self):
        for name, w in self.widgets.items():
            if isinstance(w, ToggleSwitch):
                setattr(self.cfg.faults, name, w.isChecked())
            else:
                setattr(self.cfg.faults, name, w.value())
        # in service mode the live camera lives in another process
        if self.engine is not None:
            if not self.engine.push_faults(self.cfg.name, self.cfg.faults):
                toast(self, "Could not reach the service -- fault not applied",
                      "err", 4000)

    def clear(self):
        for w in self.widgets.values():
            if isinstance(w, ToggleSwitch):
                w.setChecked(False)
            else:
                w.setValue(0)
        self.apply()


class GenerateDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate cameras")
        self.resize(520, 0)
        self.video = QLineEdit()
        self.video.textChanged.connect(lambda: self._probe.start(450))
        b = button("Browse", "folder", on_click=self._browse)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.video, 1)
        row.addWidget(b)
        vw = QWidget()
        vw.setLayout(row)
        self.vinfo = QLabel("")
        self.vinfo.setObjectName("Subtle")
        self.vinfo.setStyleSheet("font-size:11px;")

        self.count = QSpinBox()
        self.count.setRange(1, 256)
        self.count.setValue(30)
        self.name = QLineEdit("VCam {n}")
        self.name.setToolTip("{n} is replaced with the camera number")
        self.user = QLineEdit("admin")
        self.pwd = QLineEdit("admin")
        self.pwd.setEchoMode(QLineEdit.Password)
        reveal = self.pwd.addAction(icons.icon("eye", t().text_dim), QLineEdit.TrailingPosition)
        reveal.setToolTip("Show or hide password")
        reveal.setCheckable(True)
        reveal.toggled.connect(lambda on: self.pwd.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.codec = QComboBox()
        self.codec.addItems(["H264", "H265"])
        self.imp = QSpinBox()
        self.imp.setRange(0, 86400)
        self.imp.setValue(60)
        self.imp.setSuffix(" s")
        self.imp.setSpecialValueText("whole file")
        self.imp.setToolTip("Only transcode the first N seconds of the source.\n"
                            "The stream loops for ever regardless.")
        self.creds = QCheckBox("Credentials inside the RTSP URI")
        self.replace = QCheckBox("Replace the existing camera list")

        f = QFormLayout()
        f.setSpacing(9)
        f.addRow("Video file", vw)
        f.addRow("", self.vinfo)
        for lbl, w in [("Count", self.count), ("Name pattern", self.name),
                       ("Import only first", self.imp),
                       ("Username", self.user), ("Password", self.pwd),
                       ("Codec", self.codec), ("", self.creds), ("", self.replace)]:
            f.addRow(lbl, w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Generate")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        card = Card()
        card.box.addLayout(f)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.addWidget(card)
        lay.addWidget(bb)

        self._probe = QTimer(self)
        self._probe.setSingleShot(True)
        self._probe.timeout.connect(self._probe_now)

    def _probe_now(self):
        p = self.video.text().strip()
        if not p:
            self.vinfo.setText("")
        elif not Path(p).exists():
            self.vinfo.setText("file not found")
        else:
            self.vinfo.setText(media.describe(p) or "unreadable by ffprobe")

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Video file", "", "Video (*.mp4 *.mkv *.avi *.mov *.ts);;All (*)")
        if p:
            self.video.setText(p)

    def build(self) -> list[CameraCfg]:
        tpl = CameraCfg(name=self.name.text() or "VCam {n}",
                        video=self.video.text().strip(),
                        username=self.user.text(), password=self.pwd.text())
        tpl.stream_uri_credentials = self.creds.isChecked()
        tpl.import_seconds = self.imp.value()
        tpl.ensure_profiles()
        for p in tpl.profiles:
            p.codec = self.codec.currentText()
        return generate_cameras(tpl, self.count.value())


class BulkEditDialog(BaseDialog):
    """Change one or more settings on many cameras at once.

    Every field has its own "change" switch; only ticked fields are written,
    so a bulk "set bitrate to 2048" leaves names, videos and everything else
    exactly as they were. Stream fields can target the main stream, the sub
    stream or every profile."""

    def __init__(self, cameras: list[CameraCfg], selected: list[CameraCfg],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bulk edit cameras")
        self.resize(720, 640)
        self.cameras = cameras
        self.selected = selected
        self.fields: dict[str, tuple[QCheckBox, QWidget]] = {}

        enabled_n = sum(1 for c in cameras if c.enabled)
        self.scope = QComboBox()
        self.scope.addItem(f"All cameras ({len(cameras)})", "all")
        self.scope.addItem(f"Enabled cameras ({enabled_n})", "enabled")
        if selected:
            self.scope.addItem(f"Selected cameras ({len(selected)})", "selected")
            self.scope.setCurrentIndex(2 if len(selected) > 1 else 0)
        self.scope.currentIndexChanged.connect(self._update_summary)

        # ---- source & access
        src = Card("Source & access")
        fs = QFormLayout()
        fs.setSpacing(9)
        self.video = QLineEdit()
        self.video.setPlaceholderText("path to a video file")
        browse = button("Browse", "folder", on_click=self._browse)
        vrow = QHBoxLayout()
        vrow.setContentsMargins(0, 0, 0, 0)
        vrow.addWidget(self.video, 1)
        vrow.addWidget(browse)
        vw = QWidget()
        vw.setLayout(vrow)
        self._row(fs, "Video file", vw, "video", [self.video, browse])
        self.imp = QSpinBox()
        self.imp.setRange(0, 86400)
        self.imp.setValue(60)
        self.imp.setSuffix(" s")
        self.imp.setSpecialValueText("whole file")
        self._row(fs, "Import only first", self.imp, "import_seconds")
        self.user = QLineEdit("admin")
        self._row(fs, "Username", self.user, "username")
        self.pwd = QLineEdit("admin")
        self._row(fs, "Password", self.pwd, "password")
        self.creds = self._yes_no("credentials in the RTSP URI")
        self._row(fs, "Stream URI credentials", self.creds, "stream_uri_credentials")
        self.motion = QSpinBox()
        self.motion.setRange(0, 3600)
        self.motion.setSuffix(" s")
        self.motion.setSpecialValueText("off")
        self._row(fs, "Auto motion every", self.motion, "motion_interval_sec")
        self.enabled = self._yes_no("camera enabled")
        self._row(fs, "Enabled", self.enabled, "enabled")
        src.box.addLayout(fs)

        # ---- identity
        ident = Card("Identity")
        fi = QFormLayout()
        fi.setSpacing(9)
        self.manu = QLineEdit("VCamSim")
        self._row(fi, "Manufacturer", self.manu, "manufacturer")
        self.model = QLineEdit("VCS-2000")
        self._row(fi, "Model", self.model, "model")
        self.fw = QLineEdit("1.0.0")
        self._row(fi, "Firmware", self.fw, "firmware")
        ident.box.addLayout(fi)
        ident.box.addStretch()

        # ---- streams
        prof = Card("Stream profiles")
        fp = QFormLayout()
        fp.setSpacing(9)
        self.target = QComboBox()
        self.target.addItem("Main stream only (first profile)", "main")
        self.target.addItem("Sub stream only (second profile)", "sub")
        self.target.addItem("All streams", "all")
        fp.addRow("Apply stream fields to", self.target)
        self.width = QSpinBox()
        self.width.setRange(16, 7680)
        self.width.setValue(1920)
        self.width.setSingleStep(2)
        self._row(fp, "Width", self.width, "width")
        self.height = QSpinBox()
        self.height.setRange(16, 4320)
        self.height.setValue(1080)
        self.height.setSingleStep(2)
        self._row(fp, "Height", self.height, "height")
        self.fps = QSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setValue(25)
        self._row(fp, "FPS", self.fps, "fps")
        self.bitrate = QSpinBox()
        self.bitrate.setRange(32, 100000)
        self.bitrate.setValue(4096)
        self.bitrate.setSuffix(" kbps")
        self._row(fp, "Bitrate", self.bitrate, "bitrate")
        self.codec = QComboBox()
        self.codec.addItems(["H264", "H265"])
        self._row(fp, "Codec", self.codec, "codec")
        prof.box.addLayout(fp)
        prof.box.addStretch()

        self.summary = QLabel("")
        self.summary.setObjectName("Subtle")
        self.summary.setWordWrap(True)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(QLabel("Apply to"))
        top.addWidget(self.scope, 1)
        hint = QLabel("Tick a field to change it; everything else is left untouched.")
        hint.setObjectName("Subtle")

        cols = QHBoxLayout()
        cols.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(src)
        left.addWidget(ident)
        cols.addLayout(left, 1)
        cols.addWidget(prof, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(hint)
        lay.addLayout(cols, 1)
        lay.addWidget(self.summary)
        lay.addWidget(bb)
        self._update_summary()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _yes_no(what: str) -> QComboBox:
        cb = QComboBox()
        cb.addItem(f"yes - {what}", True)
        cb.addItem(f"no - {what}", False)
        return cb

    def _row(self, form: QFormLayout, label: str, widget: QWidget, key: str,
             enable: list[QWidget] | None = None):
        cb = QCheckBox(label)
        cb.setToolTip(f"Tick to change '{label}' on every targeted camera")
        targets = enable or [widget]
        for w in targets:
            w.setEnabled(False)
        cb.toggled.connect(lambda on, ws=targets: [w.setEnabled(on) for w in ws])
        cb.toggled.connect(self._update_summary)
        form.addRow(cb, widget)
        self.fields[key] = (cb, widget)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Video file", "", "Video (*.mp4 *.mkv *.avi *.mov *.ts);;All (*)")
        if p:
            self.video.setText(p)

    def targets(self) -> list[CameraCfg]:
        mode = self.scope.currentData()
        if mode == "selected":
            return list(self.selected)
        if mode == "enabled":
            return [c for c in self.cameras if c.enabled]
        return list(self.cameras)

    def changed_keys(self) -> list[str]:
        return [k for k, (cb, _) in self.fields.items() if cb.isChecked()]

    def _update_summary(self, *_):
        keys = self.changed_keys()
        n = len(self.targets())
        self.summary.setText(
            f"{len(keys)} field(s) will change on {n} camera(s)" if keys
            else f"Nothing ticked yet - {n} camera(s) targeted")

    # -------------------------------------------------------------- apply
    def apply(self) -> tuple[int, list[str]]:
        """Write the ticked fields. -> (cameras changed, warnings)."""
        keys = set(self.changed_keys())
        cams = self.targets()
        if not keys or not cams:
            return 0, []
        warnings: list[str] = []
        video = self.video.text().strip()
        if "video" in keys and not video:
            warnings.append("video file left blank -- not changed")
            keys.discard("video")
        if "video" in keys and not Path(video).exists():
            warnings.append(f"video file not found: {video}")
        stream_keys = {"width", "height", "fps", "bitrate", "codec"} & keys
        scalar = {
            "video": video, "import_seconds": self.imp.value(),
            "username": self.user.text(), "password": self.pwd.text(),
            "stream_uri_credentials": bool(self.creds.currentData()),
            "motion_interval_sec": self.motion.value(),
            "enabled": bool(self.enabled.currentData()),
            "manufacturer": self.manu.text(), "model": self.model.text(),
            "firmware": self.fw.text(),
        }
        stream_vals = {
            "width": self.width.value(), "height": self.height.value(),
            "fps": self.fps.value(), "bitrate": self.bitrate.value(),
            "codec": self.codec.currentText(),
        }
        which = self.target.currentData()
        changed = 0
        for c in cams:
            touched = False
            for k in keys & scalar.keys():
                if getattr(c, k) != scalar[k]:
                    setattr(c, k, scalar[k])
                    touched = True
            if stream_keys:
                c.ensure_profiles()
                if which == "main":
                    profs = c.profiles[:1]
                elif which == "sub":
                    profs = c.profiles[1:2]
                else:
                    profs = c.profiles
                for p in profs:
                    for k in stream_keys:
                        if getattr(p, k) != stream_vals[k]:
                            setattr(p, k, stream_vals[k])
                            touched = True
            if touched:
                changed += 1
        return changed, warnings


class SettingsDialog(BaseDialog):
    def __init__(self, cfg: AppCfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 0)
        self.cfg = cfg
        a = cfg.addressing
        self.mode = QComboBox()
        self.mode.addItems(["port", "alias"])
        self.mode.setCurrentText(a.mode)
        self.adv = QLineEdit(a.advertise_ip)
        self.adv.setPlaceholderText("blank = detect automatically")
        self.op = QSpinBox()
        self.op.setRange(1, 65000)
        self.op.setValue(a.onvif_base_port)
        self.rp = QSpinBox()
        self.rp.setRange(1, 65000)
        self.rp.setValue(a.rtsp_base_port)
        self.iface = QLineEdit(a.alias_interface)
        self.abase = QLineEdit(a.alias_base_ip)
        self.amask = QLineEdit(a.alias_netmask)
        self.mediadir = QLineEdit(cfg.media_dir)
        self.sto = QSpinBox()
        self.sto.setRange(0, 3600)
        self.sto.setValue(cfg.rtsp_session_timeout_sec)
        self.sto.setSuffix(" s")
        self.sto.setSpecialValueText("never")
        self.disc = QCheckBox("Answer WS-Discovery probes")
        self.disc.setChecked(cfg.discovery_enabled)

        net = Card("Addressing")
        fn = QFormLayout()
        fn.setSpacing(9)
        for lbl, w in [("Mode", self.mode), ("Advertise IP", self.adv),
                       ("ONVIF base port", self.op), ("RTSP base port", self.rp)]:
            fn.addRow(lbl, w)
        net.box.addLayout(fn)

        al = Card("Alias mode  (requires Administrator)")
        fa = QFormLayout()
        fa.setSpacing(9)
        for lbl, w in [("NIC name", self.iface), ("Base IP", self.abase),
                       ("Netmask", self.amask)]:
            fa.addRow(lbl, w)
        al.box.addLayout(fa)

        gen = Card("General")
        fg = QFormLayout()
        fg.setSpacing(9)
        for lbl, w in [("Media cache directory", self.mediadir),
                       ("RTSP idle session timeout", self.sto), ("", self.disc)]:
            fg.addRow(lbl, w)
        gen.box.addLayout(fg)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)
        lay.addWidget(net)
        lay.addWidget(al)
        lay.addWidget(gen)
        lay.addWidget(bb)

    def problems(self) -> list[str]:
        """Validate before apply: a bad alias IP used to surface as 'engine
        loop error' only after Start."""
        import ipaddress
        out = []
        adv = self.adv.text().strip()
        if adv:
            try:
                ipaddress.IPv4Address(adv)
            except ValueError:
                out.append(f"Advertise IP '{adv}' is not an IPv4 address")
        if self.mode.currentText() == "alias":
            for label, w in (("Base IP", self.abase), ("Netmask", self.amask)):
                txt = w.text().strip()
                if txt:
                    try:
                        ipaddress.IPv4Address(txt)
                    except ValueError:
                        out.append(f"{label} '{txt}' is not an IPv4 address")
        return out

    def apply(self):
        a = self.cfg.addressing
        a.mode = self.mode.currentText()
        a.advertise_ip = self.adv.text().strip()
        a.onvif_base_port = self.op.value()
        a.rtsp_base_port = self.rp.value()
        a.alias_interface = self.iface.text().strip() or "Ethernet"
        a.alias_base_ip = self.abase.text().strip() or "10.10.1.100"
        a.alias_netmask = self.amask.text().strip() or "255.255.255.0"
        self.cfg.rtsp_session_timeout_sec = self.sto.value()
        self.cfg.media_dir = self.mediadir.text().strip() or "media_cache"
        self.cfg.discovery_enabled = self.disc.isChecked()


# ============================================================ detail panel
class DetailPanel(QWidget):
    """Live preview + identity + endpoints for the selected camera."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.cam_name = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.setSizeConstraint(QLayout.SetMinimumSize)

        self.preview_card = Card("Live preview")
        self.preview = QLabel("no camera selected")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(150)
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.preview_card.box.addWidget(self.preview, 1)
        lay.addWidget(self.preview_card, 1)

        self.info_card = Card("Details")
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(5)
        self.grid.setColumnStretch(1, 1)
        self.info_card.box.addLayout(self.grid)
        self.rows: dict[str, QLabel] = {}
        for i, key in enumerate(["State", "Video", "Vendor", "Serial", "UUID",
                                 "MAC"]):
            k = QLabel(key)
            k.setObjectName("Subtle")
            k.setStyleSheet("font-size:11px;")
            v = QLabel("-")
            v.setObjectName("Mono")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(False)
            v.setMinimumHeight(v.fontMetrics().height() + 2)
            self.grid.addWidget(k, i, 0, Qt.AlignTop)
            self.grid.addWidget(v, i, 1)
            self.rows[key] = v
        lay.addWidget(self.info_card)

        # One row per endpoint, each independently copyable -- copying the
        # device service and every stream as one blob is rarely what you want.
        self.ep_card = Card("Endpoints")
        self.ep_grid = QGridLayout()
        self.ep_grid.setHorizontalSpacing(8)
        self.ep_grid.setVerticalSpacing(4)
        self.ep_grid.setColumnStretch(1, 1)
        self.ep_card.box.addLayout(self.ep_grid)
        self.ep_rows: list[tuple[QLabel, QLabel, QPushButton]] = []
        copy_all = button("Copy all", "copy", "ghost",
                          "Copy every endpoint for this camera")
        copy_all.clicked.connect(self._copy_all)
        self.ep_card.add_header_widget(copy_all)
        lay.addWidget(self.ep_card)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        acts.addWidget(button("Faults", "alert", tip="Open the fault console",
                              on_click=win.faults_cam))
        acts.addWidget(button("Motion", "waves", tip="Fire a 3 s motion event",
                              on_click=lambda: win.trigger("motion")))
        acts.addStretch()
        lay.addLayout(acts)
        self._urls: list[tuple[str, str]] = []

    # ------------------------------------------------------------ endpoints
    def _ep_row(self, i: int):
        """Build endpoint row `i` on demand; rows are reused between cameras."""
        while len(self.ep_rows) <= i:
            n = len(self.ep_rows)
            k = QLabel("")
            k.setObjectName("Subtle")
            k.setStyleSheet("font-size:11px;")
            k.setMinimumWidth(78)
            v = QLabel("-")
            v.setObjectName("Mono")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            b = button("", "copy", "ghost", "Copy this URL")
            b.setFixedWidth(30)
            b.clicked.connect(lambda _=False, r=n: self._copy_one(r))
            self.ep_grid.addWidget(k, n, 0, Qt.AlignTop)
            self.ep_grid.addWidget(v, n, 1)
            self.ep_grid.addWidget(b, n, 2, Qt.AlignTop)
            self.ep_rows.append((k, v, b))
        return self.ep_rows[i]

    def _copy_one(self, row: int):
        if row < len(self._urls):
            label, url = self._urls[row]
            if url and url != "-":
                QApplication.clipboard().setText(url)
                toast(self.win, f"{label} URL copied", "ok", 1800)
            else:
                toast(self.win, "Camera is not running", "warn", 1800)

    def _copy_all(self):
        live = [u for _, u in self._urls if u and u != "-"]
        if not live:
            toast(self.win, "Camera is not running", "warn", 1800)
            return
        QApplication.clipboard().setText("\n".join(live))
        toast(self.win, f"{len(live)} URLs copied", "ok")

    def clear(self):
        self.cam_name = ""
        self.preview.setPixmap(QPixmap())
        self.preview.setText("no camera selected")
        for v in self.rows.values():
            v.setText("-")
        self._urls = []
        for k, v, b in self.ep_rows:
            k.setVisible(False)
            v.setVisible(False)
            b.setVisible(False)

    def refresh(self, cfg: CameraCfg | None, rt):
        if cfg is None:
            self.clear()
            return
        self.cam_name = cfg.name
        state = rt.state if rt else ("disabled" if not cfg.enabled else "stopped")
        vid = Path(cfg.video).name if cfg.video else "(none)"
        cap = (f"  (first {cfg.import_seconds}s)"
               if cfg.import_seconds > 0 else "  (full)")
        vals = {
            "State": state, "Video": vid + (cap if cfg.video else ""),
            "Vendor": f"{cfg.manufacturer} {cfg.model} fw {cfg.firmware}",
            "Serial": cfg.serial or "-", "UUID": cfg.uuid or "-",
            "MAC": cfg.mac or "-",
        }
        fm = self.rows["State"].fontMetrics()
        width = max(120, self.info_card.width() - 130)
        for k, v in vals.items():
            lbl = self.rows[k]
            lbl.setText(fm.elidedText(str(v), Qt.ElideMiddle, width))
            lbl.setToolTip(str(v))
        col = getattr(t(), widgets.STATE_COLORS.get(state, "text_faint"))
        self.rows["State"].setStyleSheet(f"color:{col};font-weight:700;")

        running = bool(rt and rt.state == "running")
        eps = [("Device", f"{rt.base_url}/onvif/device_service" if running else "-")]
        for p in (cfg.profiles or []):
            eps.append((p.name,
                        rt.rtsp_url(p, credentials=True) if running else "-"))
        self._urls = eps
        ep_width = max(110, self.ep_card.width() - 150)
        for i, (label, url) in enumerate(eps):
            k, v, b = self._ep_row(i)
            k.setText(label)
            v.setText(fm.elidedText(url, Qt.ElideMiddle, ep_width))
            v.setToolTip(url)
            b.setEnabled(url != "-")
            for w in (k, v, b):
                w.setVisible(True)
        for i in range(len(eps), len(self.ep_rows)):
            for w in self.ep_rows[i]:
                w.setVisible(False)

    def tick_preview(self, rt):
        if rt is None or rt.state != "running" or not rt.snaps:
            if not self.preview.pixmap() or self.preview.pixmap().isNull():
                self.preview.setText("preview available while running")
            return
        data = rt.snapshot()
        if not data:
            return
        px = QPixmap()
        if not px.loadFromData(data, "JPG"):
            return
        target = self.preview.size()
        if target.width() > 8 and target.height() > 8:
            self.preview.setPixmap(px.scaled(target, Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation))


# ============================================================= main window
class MainWindow(QMainWindow):
    def __init__(self, config_path: str):
        super().__init__()
        self.config_path = config_path
        try:
            self.cfg = AppCfg.load(config_path)
        except RuntimeError as e:
            QMessageBox.critical(None, "Configuration", str(e))
            self.cfg = AppCfg()
        self.engine = None                 # LocalEngine | RemoteEngine | None
        self.svc_state = "absent"
        self._svc_ticks = 0
        self.log_lines: deque[str] = deque(maxlen=6000)
        self.log_paused = False
        self.log_filter = ""
        self._last_sig = None
        self._starting = False
        self._stopping = False
        self._stop_thread = None
        self._task_thread = None
        self._task_result = None
        self._task_done = None
        self._clean_sig = ""
        self.setWindowTitle("VCamSim  -  Virtual ONVIF Camera Simulator")
        self.resize(1420, 880)
        self.setMinimumSize(1080, 640)

        widgets.set_theme(THEMES.get(self.cfg.ui_theme, THEMES["dark"]))

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QWidget()
        self.body = body
        outer.addWidget(body, 1)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(12)
        status = QHBoxLayout()
        self.config_status = QLabel("All changes saved")
        self.config_status.setObjectName("Subtle")
        status.addWidget(self.config_status)
        status.addStretch()
        self.mode_status = QLabel("Local engine")
        self.mode_status.setObjectName("Subtle")
        status.addWidget(self.mode_status)
        status.addWidget(button("About && licenses", "link", "ghost", "Project and dependency licenses", self.about))
        bl.addLayout(status)
        bl.addLayout(self._build_stats())

        self.vsplit = QSplitter(Qt.Vertical)
        self.hsplit = QSplitter(Qt.Horizontal)
        self.hsplit.addWidget(self._build_table())
        self.detail = DetailPanel(self)
        dw = QScrollArea()
        dw.setFrameShape(QFrame.NoFrame)
        dw.setWidgetResizable(True)
        dw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        dw.setWidget(self.detail)
        dw.setMinimumWidth(300)
        self.hsplit.addWidget(dw)
        self.hsplit.setStretchFactor(0, 3)
        self.hsplit.setStretchFactor(1, 1)
        self.hsplit.setSizes([900, 380])
        self.vsplit.addWidget(self.hsplit)
        self.vsplit.addWidget(self._build_log())
        self.vsplit.setStretchFactor(0, 3)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setSizes([560, 250])
        bl.addWidget(self.vsplit, 1)

        self.overlay = BusyOverlay(body)
        self._build_shortcuts()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(400)
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._tick_preview)
        self.preview_timer.start(700)

        self.apply_theme()
        self._mark_clean()
        self.svc_state = svcctl.state()
        self._render_service()
        self.rebuild_table()
        if self.svc_state == "running":
            self._attach_service()
        self._log(f"{time.strftime('%H:%M:%S')} [gui] VCamSim ready"
                  + (f" -- service is {self.svc_state}"
                     if self.svc_state != "absent"
                     else " -- no service installed, running in-process"))

    # ------------------------------------------------------------ chrome
    def _build_header(self) -> QWidget:
        h = QFrame()
        h.setObjectName("Header")
        h.setFixedHeight(58)
        lay = QHBoxLayout(h)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)

        self.logo = QLabel()
        self.logo.setFixedSize(22, 22)
        lay.addWidget(self.logo)
        mark = QLabel("VCamSim")
        mark.setObjectName("Wordmark")
        lay.addWidget(mark)
        lay.addSpacing(6)
        lay.addWidget(vsep())
        lay.addSpacing(6)

        self.b_start = button("Start", "play", "primary", "Start every enabled camera  (F5)", self.start)
        self.b_stop = button("Stop", "stop", "", "Stop the engine  (Shift+F5)", self.stop)
        lay.addWidget(self.b_start)
        lay.addWidget(self.b_stop)
        lay.addSpacing(6)
        lay.addWidget(vsep())
        lay.addSpacing(6)

        # Edit / Remove / Faults / Copy live on every row now, so the toolbar
        # keeps only what is not per-camera. The shortcuts still work.
        for text, ic, tip, fn in [
                ("Add", "plus", "Add one camera  (Ctrl+N)", self.add_cam),
                ("Generate", "grid",
                 "Stamp out N cameras from one video  (Ctrl+G)", self.gen_cams),
                ("Bulk edit", "edit",
                 "Change resolution, bitrate, codec, video... on many cameras "
                 "at once  (Ctrl+B)", self.bulk_edit)]:
            lay.addWidget(button(text, ic, "", tip, fn))
        lay.addSpacing(6)
        lay.addWidget(vsep())
        lay.addSpacing(6)

        # ---- Windows service + firewall
        self.svc_dot = QLabel()
        self.svc_dot.setFixedSize(10, 10)
        lay.addWidget(self.svc_dot)
        self.svc_lbl = QLabel("-")
        self.svc_lbl.setObjectName("Subtle")
        lay.addWidget(self.svc_lbl)
        self.b_svc = button("", "chip", "ghost", "", self._service_menu)
        self.b_svc.setFixedWidth(34)
        lay.addWidget(self.b_svc)
        lay.addStretch()

        self.b_theme = button("", "moon", "ghost", "Switch between light and dark", self.toggle_theme)
        self.b_theme.setFixedWidth(38)
        lay.addWidget(self.b_theme)
        lay.addWidget(button("Settings", "settings", "ghost", "Addressing and general settings", self.settings))
        self.b_save = button("Save", "save", "ghost", "Save configuration and apply changes  (Ctrl+S)", self.save)
        lay.addWidget(self.b_save)
        return h

    def _build_stats(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        self.tiles = {
            "cameras": StatTile("Cameras online", "camera", "ok"),
            "clients": StatTile("Stream clients", "users", "accent"),
            "mbps": StatTile("RTP throughput", "activity", "info", " Mb/s", spark=True),
            "subs": StatTile("Event subscriptions", "bell", "purple"),
            "faults": StatTile("Faulted cameras", "alert", "warn"),
        }
        for tile in self.tiles.values():
            row.addWidget(tile, 1)
        return row

    def _build_table(self) -> QWidget:
        card = Card("Cameras")
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("Subtle")
        card.add_header_widget(self.count_lbl)
        self.camera_search = QLineEdit()
        self.camera_search.setPlaceholderText("Find a camera, IP or state…")
        self.camera_search.setClearButtonEnabled(True)
        self.camera_search.setAccessibleName("Filter cameras")
        self.camera_search.textChanged.connect(self._filter_cameras)
        card.box.addWidget(self.camera_search)

        self.empty_state = QWidget()
        empty = QVBoxLayout(self.empty_state)
        empty.addStretch()
        title = QLabel("Create your first virtual camera")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px;font-weight:600;")
        empty.addWidget(title)
        hint = QLabel("Choose a video, generate cameras, then press Start.\nThe source is imported once and loops continuously.")
        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName("Subtle")
        empty.addWidget(hint)
        cta = QHBoxLayout()
        cta.addStretch()
        cta.addWidget(button("Generate cameras", "plus", "primary", "Choose a video and camera count", self.gen_cams))
        cta.addStretch()
        empty.addLayout(cta)
        empty.addStretch()
        card.box.addWidget(self.empty_state)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Ctrl / Shift-click selects several rows for Bulk edit; everything
        # else keys off the current row, so single-click behaviour is unchanged
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.horizontalHeader().setHighlightSections(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(COLS.index("Error"), QHeaderView.Stretch)
        hh.setSectionResizeMode(ACT_COL, QHeaderView.Fixed)
        self.table.setColumnWidth(ACT_COL, 134)
        hh.setMinimumSectionSize(64)
        for c, kind in enumerate(COL_KIND):
            it = self.table.horizontalHeaderItem(c)
            it.setTextAlignment(Qt.AlignVCenter | (
                Qt.AlignRight if kind == "num" else Qt.AlignLeft))
        self.delegate = RowDelegate(self.table, self)
        self.table.setItemDelegate(self.delegate)
        self.table.entered.connect(
            lambda idx: self._hover(idx.row()))
        self.table.viewport().installEventFilter(self)
        self.table.doubleClicked.connect(self.edit_cam)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        card.box.addWidget(self.table)
        return card

    def _build_log(self) -> QWidget:
        card = Card("Activity log")
        self.search = QLineEdit()
        self.search.setPlaceholderText("filter...")
        self.search.setFixedWidth(190)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._set_filter)
        card.add_header_widget(self.search)
        self.b_pause = button("", "pause", "ghost", "Pause the log", self._toggle_pause)
        self.b_pause.setCheckable(True)
        self.b_pause.setFixedWidth(34)
        card.add_header_widget(self.b_pause)
        card.add_header_widget(button("", "trash", "ghost", "Clear the log", self._clear_log)).setFixedWidth(34)
        card.add_header_widget(button("", "copy", "ghost", "Copy the log", self._copy_log)).setFixedWidth(34)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setWordWrapMode(self.log.wordWrapMode())
        card.box.addWidget(self.log)
        return card

    def _build_shortcuts(self):
        for seq, fn in [("F5", self.start), ("Shift+F5", self.stop),
                        ("Ctrl+S", self.save), ("Ctrl+N", self.add_cam),
                        ("Ctrl+G", self.gen_cams), ("Ctrl+E", self.edit_cam),
                        ("Ctrl+B", self.bulk_edit),
                        ("Delete", self.del_cam), ("Ctrl+D", self.faults_cam),
                        ("Ctrl+L", lambda: self.search.setFocus()),
                        ("Ctrl+T", self.toggle_theme)]:
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

    # ------------------------------------------------------------- theme
    def apply_theme(self):
        th = t()
        QApplication.instance().setStyleSheet(stylesheet(th))
        self.logo.setPixmap(icons.pixmap("camera", th.accent, 22))
        # set the property, not the icon: the loop below re-renders every
        # button's icon from `icon_name` and would otherwise overwrite it.
        self.b_theme.setProperty("icon_name",
                                 "sun" if th.name == "dark" else "moon")
        self.b_theme.setToolTip("Switch to the "
                                + ("light" if th.name == "dark" else "dark")
                                + " theme  (Ctrl+T)")
        for tile in self.tiles.values():
            tile.restyle()
        for b in self.findChildren(QPushButton):
            ic = b.property("icon_name")
            if ic:
                col = "#ffffff" if b.property("kind") == "primary" else th.text_dim
                b.setIcon(icons.icon(ic, col, 16))
            # a changed dynamic property needs an explicit re-polish
            b.style().unpolish(b)
            b.style().polish(b)
        self.table.viewport().update()
        self._render_log()

    def toggle_theme(self):
        nxt = "light" if t().name == "dark" else "dark"
        self.cfg.ui_theme = nxt
        widgets.set_theme(THEMES[nxt])
        self.apply_theme()
        toast(self, f"{nxt.capitalize()} theme", "info", 1600)

    # --------------------------------------------------------- selection
    def eventFilter(self, obj, ev):
        if obj is self.table.viewport() and ev.type() == ev.Type.Leave:
            self._hover(-1)
        return super().eventFilter(obj, ev)

    def _hover(self, row: int):
        if self.delegate.hover_row != row:
            self.delegate.hover_row = row
            self.table.viewport().update()

    def sel_index(self) -> int:
        r = self.table.currentRow()
        return r if 0 <= r < len(self.cfg.cameras) else -1

    def sel_cfg(self) -> CameraCfg | None:
        i = self.sel_index()
        return self.cfg.cameras[i] if i >= 0 else None

    def sel_cfgs(self) -> list[CameraCfg]:
        """Every selected row (multi-select), in table order."""
        rows = sorted({ix.row() for ix in self.table.selectionModel().selectedRows()})
        return [self.cfg.cameras[r] for r in rows if 0 <= r < len(self.cfg.cameras)
                and not self.table.isRowHidden(r)]

    def bulk_edit(self):
        if self._task_thread is not None or self._stopping:
            return
        if not self.cfg.cameras:
            toast(self, "No cameras configured -- press Generate", "warn")
            return
        d = BulkEditDialog(self.cfg.cameras, self.sel_cfgs(), self)
        if d.exec() != QDialog.Accepted:
            return
        changed, warnings = d.apply()
        for note in self.cfg.normalize():
            warnings.append(note)
        self.rebuild_table()
        for w in warnings[:3]:
            toast(self, w, "warn", 5000)
        if changed == 0:
            toast(self, "Nothing changed", "info")
            return
        toast(self, f"{changed} camera(s) updated", "ok")
        if self.engine and self.engine.running:
            toast(self, "Press Save to apply the changes to the running engine",
                  "warn", 4500)

    def _runtime(self, cfg: CameraCfg | None):
        if cfg is None or not self.engine:
            return None
        return self.engine.camera(cfg.name)

    def _selection_changed(self):
        c = self.sel_cfg()
        self.detail.refresh(c, self._runtime(c))

    # ----------------------------------------------------------- actions
    def _free_name(self, base: str, skip: CameraCfg | None = None) -> str:
        taken = {c.name for c in self.cfg.cameras if c is not skip}
        if base not in taken:
            return base
        n = 2
        while f"{base} ({n})" in taken:
            n += 1
        return f"{base} ({n})"

    def _run_camera_dialog(self, cfg: CameraCfg, skip: CameraCfg | None):
        """Open the dialog until the result is acceptable or cancelled.

        Identity (UUID/MAC/serial) is derived from the name, so two cameras
        with one name would be a single device to a VMS -- refuse it here
        rather than at Start, and keep the user's edits in the dialog."""
        d = CameraDialog(cfg, self)
        while d.exec() == QDialog.Accepted:
            res = d.result_cfg()
            if any(c.name == res.name for c in self.cfg.cameras if c is not skip):
                QMessageBox.warning(self, "Camera",
                                    f"There is already a camera named "
                                    f"'{res.name}'. Pick another name.")
                d.name.setFocus()
                d.name.selectAll()
                continue
            if not res.video:
                if QMessageBox.question(
                        self, "Camera", "No video file is set -- this camera "
                        "cannot start until it has one.\n\nKeep it anyway?"
                        ) != QMessageBox.Yes:
                    continue
            return res
        return None

    def add_cam(self):
        if self._task_thread is not None or self._stopping:
            return
        c = CameraCfg(name=self._free_name(f"VCam {len(self.cfg.cameras) + 1:02d}"))
        c.ensure_profiles()
        res = self._run_camera_dialog(c, None)
        if res is not None:
            self.cfg.cameras.append(res)
            self.rebuild_table()
            self.table.selectRow(len(self.cfg.cameras) - 1)
            toast(self, "Camera added", "ok")

    def gen_cams(self):
        if self._task_thread is not None or self._stopping:
            return
        d = GenerateDialog(self)
        if d.exec() != QDialog.Accepted:
            return
        cams = d.build()
        if not cams or not cams[0].video:
            QMessageBox.warning(self, "Generate", "Pick a video file first.")
            return
        if not Path(cams[0].video).exists():
            QMessageBox.warning(self, "Generate",
                                f"Video file not found:\n{cams[0].video}")
            return
        if d.replace.isChecked():
            self.cfg.cameras = cams
        else:
            # generating a second batch must not reuse "VCam 01" and collide
            # with the batch already in the list
            for c in cams:
                c.name = self._free_name(c.name)
                self.cfg.cameras.append(c)
        self.rebuild_table()
        toast(self, f"{len(cams)} cameras generated", "ok")

    def edit_cam(self):
        if self._task_thread is not None or self._stopping:
            return
        i = self.sel_index()
        if i < 0:
            return
        cur = self.cfg.cameras[i]
        res = self._run_camera_dialog(cur, cur)
        if res is not None:
            self.cfg.cameras[i] = res
            self.rebuild_table()
            self.table.selectRow(i)
            if self.engine and self.engine.running:
                toast(self, "Press Save to apply the change to the running "
                            "engine", "warn", 4500)

    def del_cam(self):
        if self._task_thread is not None or self._stopping:
            return
        i = self.sel_index()
        if i < 0:
            return
        name = self.cfg.cameras[i].name
        if QMessageBox.question(self, "Remove camera",
                                f"Remove '{name}'?") != QMessageBox.Yes:
            return
        self.cfg.cameras.pop(i)
        self.rebuild_table()
        toast(self, f"Removed {name}", "info")

    def faults_cam(self):
        if self._task_thread is not None or self._stopping:
            return
        c = self.sel_cfg()
        if not c:
            toast(self, "Select a camera first", "warn")
            return
        rt = self._runtime(c)
        FaultDialog(c, self.engine, self).exec()
        self.refresh()

    def trigger(self, kind: str):
        c = self.sel_cfg()
        if not c:
            toast(self, "Select a camera first", "warn")
            return
        if not self.engine or not self.engine.running:
            toast(self, "Start the engine first", "warn")
            return
        if kind == "motion":
            name = c.name
            self.engine.trigger(name, "motion", True)
            QTimer.singleShot(3000, lambda: self.engine and
                              self.engine.trigger(name, "motion", False))
            toast(self, f"Motion fired on {name}", "ok", 2000)

    def copy_urls(self):
        c = self.sel_cfg()
        if not c:
            toast(self, "Select a camera first", "warn")
            return
        self._row_copy_menu(c)

    def fix_firewall(self):
        if firewall.rule_exists():
            r = QMessageBox.question(
                self, "Windows Firewall",
                "A VCamSim firewall rule already exists.\n\n"
                "Re-create it? (choose No to remove it)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.No:
                ok, msg = firewall.remove_rule()
                toast(self, "Firewall rule removed" if ok else f"Failed: {msg}",
                      "ok" if ok else "err")
                return
        if not is_admin():
            QMessageBox.warning(
                self, "Administrator required",
                "Adding a firewall rule needs Administrator rights.\n\n"
                "Close the app, right-click it and choose "
                "'Run as administrator', then press Firewall again.\n\n"
                "Without this rule Windows blocks inbound ONVIF/RTSP from "
                "OTHER machines - it will still work from this PC only.")
            return
        ok, msg = firewall.add_rule()
        toast(self, "Firewall rule added" if ok else f"Failed: {msg}",
              "ok" if ok else "err", 5000)

    def settings(self):
        if self._task_thread is not None or self._stopping:
            return
        d = SettingsDialog(self.cfg, self)
        while d.exec() == QDialog.Accepted:
            bad = d.problems()
            if bad:
                QMessageBox.warning(self, "Settings", "\n".join(bad))
                continue
            d.apply()
            toast(self, "Settings applied", "ok")
            break

    def about(self):
        QMessageBox.about(self, "About VCamSim",
            "<b>VCamSim</b> — Virtual ONVIF Camera Simulator<br><br>"
            "A lab tool for discovery, streaming and VMS fault testing.<br>"
            "Uses PySide6 / Qt (LGPLv3), PyYAML (MIT), and Lucide / Feather icons. "
            "FFmpeg is an external program with its own license.<br><br>"
            "See LICENSE and THIRD_PARTY_NOTICES.md in the project folder "
            "for license terms and source links.")

    def _run_task(self, label, work, done):
        """Run a slow operation off Qt's thread; finish on the UI timer."""
        if self._task_thread is not None or self._stopping:
            return
        self._task_result = None
        self._task_done = done
        def run():
            try:
                if work() is False:
                    raise RuntimeError("Operation failed. Your edits are retained; check the activity log.")
                self._task_result = (True, "")
            except Exception as e:
                self._task_result = (False, str(e))
        self._task_thread = threading.Thread(target=run, daemon=True, name="vcamsim-action")
        self.overlay.start(label, "Your changes are kept until the operation succeeds")
        self._task_thread.start()
        self._sync_buttons()
        QTimer.singleShot(50, self._poll_task)

    def _poll_task(self):
        if self._task_thread is not None and self._task_thread.is_alive():
            QTimer.singleShot(50, self._poll_task)
            return
        ok, message = self._task_result or (False, "Operation did not finish")
        done = self._task_done
        self._task_thread = self._task_done = None
        self.overlay.stop()
        if ok:
            done()
        else:
            self._starting = False
            self._log(f"{time.strftime('%H:%M:%S')} [gui] {message}")
            toast(self, message, "err", 7000)
        self._sync_buttons()

    def save(self):
        if self._task_thread is not None or self._stopping:
            return
        self.cfg.normalize()
        signature = self._signature()
        snapshot = from_dict(AppCfg, to_dict(self.cfg))
        eng = self.engine
        def work():
            if eng is not None and eng.mode == "service":
                return eng.client.put_config(to_dict(snapshot))
            snapshot.save(self.config_path)
            if eng is not None and eng.running:
                # The running engine owns a snapshot; edits never alter it halfway.
                eng.cfg = snapshot
                return eng.push_config()
            return True
        def done():
            self._clean_sig = signature
            if eng is not None:
                eng.cfg = self.cfg
            self._starting = bool(eng and eng.running)
            if self._starting:
                self.overlay.start("Applying configuration", "Preparing changed media")
            toast(self, "Configuration saved and applied" if eng else "Configuration saved", "ok")
        self._run_task("Saving configuration", work, done)

    # ----------------------------------------------------------- service
    def _poll_service(self):
        """Track the service and attach to it whenever it is up.

        `sc query` is a process spawn, so an absent service is re-checked
        rarely -- running from source must not spawn one twice a second."""
        if self._task_thread is not None or self._stopping:
            return
        self._svc_ticks += 1
        if self._svc_ticks % 8 != 0:
            # in between, a dead service shows up as a failed refresh() in
            # tick() -- no need to spawn sc.exe 2.5 times a second
            return
        prev = self.svc_state
        self.svc_state = svcctl.state()
        if self.svc_state != prev:
            self._render_service()
        if self.svc_state == "running":
            if self.engine is None or self.engine.mode != "service":
                self._attach_service()
        elif self.engine is not None and self.engine.mode == "service":
            self._log(f"{time.strftime('%H:%M:%S')} [gui] service stopped")
            self.engine = None
            self.refresh()

    def _attach_service(self):
        if self._task_thread is not None or self._stopping:
            return
        if self.engine is not None and self.engine.mode == "local":
            if self.engine.running:
                return
        eng = RemoteEngine(self.cfg, self.cfg.control_port)
        if not eng.refresh():
            return
        if self._dirty():
            choice = QMessageBox.question(self, "Unsaved changes",
                "The service is running. Send your unsaved changes to it?\n\n"
                "Yes applies your edits. No loads the service configuration. "
                "Cancel keeps this window unchanged.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if choice == QMessageBox.Cancel:
                return
            if choice == QMessageBox.Yes:
                signature = self._signature()
                payload = to_dict(self.cfg)
                def done():
                    self.engine = eng
                    self._clean_sig = signature
                    self.rebuild_table()
                    toast(self, "Configuration sent to the service", "ok")
                self._run_task("Connecting to the service", lambda: eng.client.put_config(payload), done)
                return
        try:
            cfg = from_dict(AppCfg, eng.client.get_config())
            cfg.normalize()
        except Exception as e:
            toast(self, f"Could not read service configuration: {e}", "err", 6000)
            return
        self.cfg = eng.cfg = cfg
        self.engine = eng
        self._mark_clean()
        err = eng.stats().get("config_error")
        if err:
            self._log(f"{time.strftime('%H:%M:%S')} [gui] SERVICE CONFIG ERROR: {err}")
        self._log(f"{time.strftime('%H:%M:%S')} [gui] attached to the VCamSim service")
        self.rebuild_table()

    # --------------------------------------------------------- dirty state
    def _signature(self) -> str:
        import json
        d = to_dict(self.cfg)
        d.pop("ui_theme", None)              # a theme flip is not an edit
        for camera in d.get("cameras", []):
            for key in ("ip", "onvif_port", "rtsp_port"):
                camera.pop(key, None)
        return json.dumps(d, sort_keys=True, default=str)

    def _mark_clean(self):
        self._clean_sig = self._signature()

    def _dirty(self) -> bool:
        try:
            return self._signature() != self._clean_sig
        except Exception:
            return False

    def _render_service(self):
        th = t()
        colors = {"running": th.ok, "starting": th.warn, "stopping": th.warn,
                  "stopped": th.text_faint, "absent": th.text_faint}
        col = colors.get(self.svc_state, th.text_faint)
        px = QPixmap(10, 10)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor(col))
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, 10, 10)
        p.end()
        self.svc_dot.setPixmap(px)
        text = ("no service" if self.svc_state == "absent" else
                f"service {self.svc_state}")
        self.svc_lbl.setText(text)
        self.svc_lbl.setStyleSheet(f"color:{col};font-weight:600;")
        self.b_svc.setToolTip(f"Windows service ({text}) and firewall")

    def _service_menu(self):
        m = QMenu(self)
        st = self.svc_state
        if st == "absent":
            m.addAction("Install service").triggered.connect(
                lambda: self._service_do("install"))
            a = m.addAction("(installs and starts at boot)")
            a.setEnabled(False)
        else:
            if st != "running":
                m.addAction("Start service").triggered.connect(
                    lambda: self._service_do("start"))
            if st in ("running", "starting"):
                m.addAction("Stop service").triggered.connect(
                    lambda: self._service_do("stop"))
                m.addAction("Restart service").triggered.connect(
                    lambda: self._service_do("restart"))
            m.addSeparator()
            m.addAction("Remove service").triggered.connect(
                lambda: self._service_do("remove"))
        m.addSeparator()
        m.addAction("Windows Firewall rule...").triggered.connect(
            self.fix_firewall)
        m.exec(QCursor.pos())

    def _service_do(self, action: str):
        if not is_admin():
            QMessageBox.warning(
                self, "Administrator required",
                f"Changing the Windows service ({action}) needs Administrator "
                "rights.\n\nClose VCamSim, right-click it and choose "
                "'Run as administrator', then try again.")
            return
        if action == "restart":
            svcctl.stop()
            time.sleep(1.5)
            ok, msg = svcctl.start()
        else:
            ok, msg = {"install": svcctl.install, "remove": svcctl.remove,
                       "start": svcctl.start, "stop": svcctl.stop}[action]()
        if ok and action == "install":
            svcctl.start()
        toast(self, msg if ok else f"Failed: {msg}", "ok" if ok else "err",
              5000 if not ok else 3000)
        self._svc_ticks = 0
        self.svc_state = svcctl.state()
        self._render_service()

    # ------------------------------------------------------------ engine
    def start(self):
        if self.engine and self.engine.running:
            return
        if self._stopping:
            toast(self, "Still stopping -- one moment", "warn")
            return
        if not self.cfg.cameras:
            toast(self, "No cameras configured -- press Generate", "warn")
            return
        if not any(c.enabled for c in self.cfg.cameras):
            toast(self, "Every camera is disabled", "warn")
            return
        if self._task_thread is not None:
            return
        if self.engine is not None and self.engine.mode == "service":
            eng = self.engine
            signature = self._signature()
            payload = to_dict(self.cfg)
            def work():
                if not eng.client.put_config(payload):
                    return False
                return eng.client.engine("start")
            def done():
                self._clean_sig = signature
                self._starting = True
                self.overlay.start("Starting cameras", "Preparing media in the service")
            self._run_task("Starting cameras", work, done)
            return
        if self.svc_state == "running":
            toast(self, "The service owns the ports -- stop it first", "warn",
                  4500)
            return
        self.engine = LocalEngine(self.cfg)
        self._starting = True
        try:
            self.engine.start()
        except Exception as e:
            self.engine = None
            self._starting = False
            QMessageBox.critical(self, "Start failed", str(e))
            return
        self.overlay.start("Preparing media",
                           "first run transcodes each profile once")
        self._sync_buttons()

    def stop(self, wait: bool = False):
        """Stop the engine without freezing the window.

        Engine.stop() joins the engine thread (up to 15 s while a transcode is
        killed and 60 sockets close) and in service mode the control call
        takes just as long. Run it on a worker and let the overlay animate;
        `wait=True` (window close) blocks instead."""
        if not self.engine or self._stopping or self._task_thread is not None:
            return
        eng = self.engine
        remote = eng.mode == "service"
        self._stopping = True
        self._starting = False
        self.overlay.start("Stopping", "closing sockets and sessions")
        self._sync_buttons()
        if wait:
            try:
                eng.stop()
            finally:
                self._finish_stop(eng, remote)
            return
        import threading
        self._stop_error = ""

        def work():
            try:
                if eng.stop() is False:
                    self._stop_error = "The service did not confirm that the engine stopped."
            except Exception as e:                       # pragma: no cover
                self._stop_error = str(e)
        self._stop_thread = threading.Thread(target=work, daemon=True,
                                             name="vcamsim-stop")
        self._stop_thread.start()
        self._stop_ctx = (eng, remote)
        QTimer.singleShot(100, self._poll_stop)

    def _poll_stop(self):
        if self._stop_thread is not None and self._stop_thread.is_alive():
            QTimer.singleShot(100, self._poll_stop)
            return
        self._stop_thread = None
        eng, remote = self._stop_ctx
        if self._stop_error:
            self._stopping = False
            self.overlay.stop()
            self._log(f"{time.strftime('%H:%M:%S')} [gui] stop failed: {self._stop_error}")
            toast(self, "Stop failed. Check the activity log and try again.", "err", 6000)
            self._sync_buttons()
            return
        self._finish_stop(eng, remote)

    def _finish_stop(self, eng, remote: bool):
        self._stopping = False
        try:
            for line in eng.drain_logs():
                self._log(line)
        except Exception:
            pass
        if not remote and eng.engine.thread and eng.engine.thread.is_alive():
            self.overlay.stop()
            toast(self, "Engine is still stopping. Wait a moment and press Stop again.", "warn", 6000)
            self._sync_buttons()
            return
        if not remote and self.engine is eng:
            self.engine = None       # keep the proxy attached in service mode
        self.overlay.stop()
        for tile in self.tiles.values():
            tile.set_value(0)
        self.tiles["mbps"].spark.reset()
        self.refresh()
        self._sync_buttons()
        toast(self, "Engine stopped", "info")

    def _sync_buttons(self):
        live = bool(self.engine and self.engine.running)
        busy = self._task_thread is not None or self._stopping
        dirty = self._dirty()
        self.b_start.setEnabled(not live and not busy and any(c.enabled for c in self.cfg.cameras))
        self.b_stop.setEnabled(live and not busy)
        self.b_save.setEnabled(not busy)
        self.b_save.setText("Save && apply" if live else "Save")
        self.config_status.setText("Unsaved changes" if dirty else "All changes saved")
        self.config_status.setStyleSheet(f"color:{t().warn if dirty else t().text_dim};")
        self.mode_status.setText("Windows service" if self.engine and self.engine.mode == "service" else "Local engine")
        self.setWindowTitle("VCamSim — Virtual ONVIF Camera Simulator" + (" *" if dirty else ""))

    def _filter_cameras(self, *_):
        needle = self.camera_search.text().strip().casefold()
        for row in range(self.table.rowCount()):
            text = " ".join(self.table.item(row, col).text() for col in (0, 1, 4)
                            if self.table.item(row, col) is not None)
            hidden = bool(needle and needle not in text.casefold())
            self.table.setRowHidden(row, hidden)
            if hidden:
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item is not None:
                        item.setSelected(False)

    # ------------------------------------------------------------ render
    def tick(self):
        if self._task_thread is not None:
            self._sync_buttons()
            return
        self._poll_service()
        eng = self.engine
        if self._stopping:
            # the worker owns the engine until it is done; reading stats
            # from a loop that is being torn down is pointless
            self._sync_buttons()
            return
        if eng is not None and eng.mode == "service" and not eng.refresh():
            eng = None                       # service went away mid-poll
        if eng:
            for line in eng.drain_logs(300):
                self._log(line)
            st = eng.stats()
            phase = st["phase"]
            if phase == "preparing":
                done, total = st["progress"]
                frac, label = st.get("item_progress") or (0.0, "")
                overall = ((done + frac) / total) if total else -1.0
                self.overlay.update_text(
                    f"Preparing media   {overall * 100:.0f}%" if overall >= 0
                    else "Preparing media",
                    f"camera {min(done + 1, total)} of {total}"
                    + (f"  -  {label}" if label else ""),
                    overall)
            elif phase == "starting":
                self.overlay.update_text("Starting servers", "binding sockets",
                                         -1.0)
            elif self.overlay.isVisible():
                self.overlay.stop()
                if self._starting:
                    self._starting = False
                    n, tot = st["running"], st["total"]
                    toast(self, f"{n}/{tot} cameras online",
                          "ok" if n == tot else "warn", 3500)
            if not eng.running and phase == "idle" and self._starting:
                self._starting = False
                self.overlay.stop()
                toast(self, "Engine stopped before it came up", "err")
            self._update_stats(st)
            if eng.take_dirty():
                self.refresh()
            else:
                self.refresh(light=True)
        self._sync_buttons()

    def _update_stats(self, st: dict):
        self.tiles["cameras"].set_value(st["running"],
                                        f"of {st['total']} configured")
        self.tiles["clients"].set_value(st["clients"], "RTSP sessions playing")
        mbps = st["mbps"]
        if mbps is not None:
            self.tiles["mbps"].set_value(mbps, "outbound RTP", decimals=2)
            self.tiles["mbps"].push_spark(mbps)
        self.tiles["subs"].set_value(st["subs"], "PullPoint subscriptions")
        self.tiles["faults"].set_value(
            st["faults"], "with fault injection on" if st["faults"] else "all clean")

    def rebuild_table(self):
        # Shrinking rowCount does not reliably destroy cell widgets: the old
        # action buttons stay parented to the viewport and keep painting over
        # empty rows. Take them out explicitly first.
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, ACT_COL)
            if w is not None:
                self.table.removeCellWidget(r, ACT_COL)
                w.setParent(None)
                w.deleteLater()
        self.table.setRowCount(len(self.cfg.cameras))
        self.empty_state.setVisible(not self.cfg.cameras)
        self.table.setVisible(bool(self.cfg.cameras))
        self.camera_search.setVisible(bool(self.cfg.cameras))
        for r, cam in enumerate(self.cfg.cameras):
            for c in range(len(COLS)):
                it = QTableWidgetItem("")
                it.setData(RowDelegate.KIND, COL_KIND[c])
                self.table.setItem(r, c, it)
            self.table.setCellWidget(r, ACT_COL, self._row_actions(cam))
        self._last_sig = None
        self.refresh()

    def _row_actions(self, cam: CameraCfg) -> QWidget:
        """Per-row Edit / Faults / Copy / Remove, bound to this camera object.

        Bound to the CameraCfg rather than a row index so the buttons keep
        pointing at the right camera even if the list is reordered."""
        w = QWidget()
        w.setObjectName("RowActions")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(1)
        lay.addStretch()
        for ic, tip, fn in [
                ("copy", "Copy a URL from this camera",
                 lambda: self._row_copy_menu(cam)),
                ("alert", "Fault console for this camera",
                 lambda: self._row_faults(cam)),
                ("edit", "Edit this camera", lambda: self._row_edit(cam)),
                ("trash", "Remove this camera", lambda: self._row_del(cam))]:
            b = button("", ic, "ghost", tip, fn)
            b.setFixedSize(26, 26)
            b.setIconSize(QSize(15, 15))
            lay.addWidget(b)
        return w

    def _row_index(self, cam: CameraCfg) -> int:
        for i, c in enumerate(self.cfg.cameras):
            if c is cam:
                return i
        return -1

    def _select(self, cam: CameraCfg) -> int:
        i = self._row_index(cam)
        if i >= 0:
            self.table.selectRow(i)
        return i

    def _row_edit(self, cam: CameraCfg):
        if self._select(cam) >= 0:
            self.edit_cam()

    def _row_del(self, cam: CameraCfg):
        if self._select(cam) >= 0:
            self.del_cam()

    def _row_faults(self, cam: CameraCfg):
        if self._select(cam) >= 0:
            self.faults_cam()

    def _row_copy_menu(self, cam: CameraCfg):
        """One entry per endpoint, so a single stream can be copied alone."""
        self._select(cam)
        rt = self._runtime(cam)
        running = bool(rt and rt.state == "running")
        m = QMenu(self)
        if not running:
            a = m.addAction("camera is not running")
            a.setEnabled(False)
        else:
            eps = [("Device service", f"{rt.base_url}/onvif/device_service")]
            eps += [(f"{p.name}  ({p.codec} {p.width}x{p.height})",
                     rt.rtsp_url(p, credentials=True)) for p in cam.profiles]
            for label, url in eps:
                act = m.addAction(label)
                act.setToolTip(url)
                act.triggered.connect(
                    lambda _=False, u=url, l=label: self._copy(u, l))
            m.addSeparator()
            allu = "\n".join(u for _, u in eps)
            m.addAction("All of the above").triggered.connect(
                lambda: self._copy(allu, f"{len(eps)} URLs"))
        m.exec(QCursor.pos())

    def _copy(self, text: str, what: str):
        QApplication.clipboard().setText(text)
        toast(self, f"{what} copied", "ok", 1800)

    def refresh(self, light: bool = False):
        if len(self.cfg.cameras) != self.table.rowCount():
            self.rebuild_table()
            return
        st = {s["name"]: s for s in (self.engine.status() if self.engine else [])}
        for r, c in enumerate(self.cfg.cameras):
            s = st.get(c.name, {})
            state = s.get("state") or ("disabled" if not c.enabled else "stopped")
            vals = [c.name,
                    s.get("ip") or "-",
                    s.get("onvif") or "-",
                    s.get("rtsp") or "-",
                    state,
                    s.get("clients", 0), s.get("subs", 0),
                    "FAULT" if (s.get("faults") or c.faults.any_active()) else "",
                    s.get("error", "")]
            for col, v in enumerate(vals):
                it = self.table.item(r, col)
                txt = str(v)
                if it is not None and it.text() != txt:
                    it.setText(txt)
                    # the delegate elides; the full value stays reachable
                    it.setToolTip(txt if len(txt) > 12 else "")
        n = sum(1 for s in st.values() if s["state"] == "running")
        self.count_lbl.setText(f"{n} running / {len(self.cfg.cameras)} configured"
                               f"   -   {self.cfg.addressing.mode} mode")
        self._filter_cameras()
        if not light:
            c = self.sel_cfg()
            self.detail.refresh(c, self._runtime(c))

    def _tick_preview(self):
        c = self.sel_cfg()
        self.detail.tick_preview(self._runtime(c))

    # --------------------------------------------------------------- log
    def _log(self, line: str):
        self.log_lines.append(line)
        if self.log_paused:
            return
        if self.log_filter and self.log_filter not in line.lower():
            return
        self._emit(line)

    def _emit(self, line: str):
        th = t()
        low = line.lower()
        if any(k in low for k in ("error", "fail", "denied", "warning", "no ")):
            col = th.err if ("error" in low or "fail" in low) else th.warn
        elif any(k in low for k in ("listening", "started", "ok", "added")):
            col = th.ok
        else:
            col = th.text_dim
        esc = (line.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        parts = esc.split(" ", 1)
        ts = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if rest.startswith("[") and "]" in rest:
            who, _, msg = rest[1:].partition("]")
            rest = (f'<span style="color:{th.accent}">[{who}]</span>'
                    f'<span style="color:{col}">{msg}</span>')
        else:
            rest = f'<span style="color:{col}">{rest}</span>'
        self.log.appendHtml(
            f'<span style="color:{th.text_faint}">{ts}</span> {rest}')

    def _render_log(self):
        self.log.clear()
        f = self.log_filter
        for line in self.log_lines:
            if not f or f in line.lower():
                self._emit(line)

    def _set_filter(self, text: str):
        self.log_filter = text.strip().lower()
        self._render_log()

    def _toggle_pause(self):
        self.log_paused = self.b_pause.isChecked()
        if not self.log_paused:
            self._render_log()

    def _clear_log(self):
        self.log_lines.clear()
        self.log.clear()

    def _copy_log(self):
        QApplication.clipboard().setText("\n".join(self.log_lines))
        toast(self, "Log copied", "ok", 1600)

    # ------------------------------------------------------------ window
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.overlay.isVisible():
            self.overlay.resize(self.body.size())

    def closeEvent(self, ev):
        if self._task_thread is not None:
            toast(self, "Wait for the current operation before closing", "warn")
            ev.ignore()
            return
        remote = self.engine is not None and self.engine.mode == "service"
        if remote and self._dirty():
            # in service mode the service owns config.yaml, so an implicit
            # save on close is not possible -- ask instead of losing edits
            r = QMessageBox.question(
                self, "Unsaved changes",
                "Send the changed configuration to the service before "
                "closing?\n\n(The service restarts its engine to apply it.)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Yes and not self.engine.push_config():
                if QMessageBox.question(
                        self, "Unsaved changes",
                        "The service did not answer. Close anyway and lose "
                        "the changes?") != QMessageBox.Yes:
                    ev.ignore()
                    return
        self.timer.stop()
        self.preview_timer.stop()
        if self._stop_thread is not None and self._stop_thread.is_alive():
            self._stop_thread.join(timeout=20)
        if self.engine and not remote:
            self.stop(wait=True)        # never stop the service on window close
        self.engine = None
        if not remote:
            try:
                self.cfg.save(self.config_path)
            except Exception as e:
                QMessageBox.warning(self, "Save failed",
                                    f"Could not write {self.config_path}:\n{e}")
        ev.accept()


def _install_crash_log():
    """The exe has no console, so an uncaught exception in a Qt slot would
    vanish. Write it to vcamsim-crash.log beside the program (and stderr when
    there is one) and keep running -- one bad slot must not kill 30 cameras."""
    import threading
    import traceback
    from .config import app_dir

    def record(kind: str, exc_type, exc, tb):
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{kind}]\n{txt}\n"
        try:
            with open(app_dir() / "vcamsim-crash.log", "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        try:
            sys.__stderr__ and sys.__stderr__.write(line)
        except Exception:
            pass

    def hook(exc_type, exc, tb):
        record("main thread", exc_type, exc, tb)

    def thook(args):
        record(f"thread {args.thread.name if args.thread else '?'}",
               args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thook


def main(config_path: str = "config.yaml", autostart: bool = False):
    _install_crash_log()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("VCamSim")
    app.setApplicationDisplayName("VCamSim")
    app.setWindowIcon(icons.icon("camera", "#4f8cff", 64))
    f = QFont()
    f.setPointSizeF(9.5)
    app.setFont(f)
    w = MainWindow(config_path)
    w.show()
    if autostart:
        QTimer.singleShot(400, w.start)
    sys.exit(app.exec())
