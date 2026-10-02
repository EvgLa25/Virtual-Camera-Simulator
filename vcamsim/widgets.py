"""Custom animated widgets shared by the UI.

Everything here paints itself from the active `theme.Theme`, so switching
between light and dark is a single `set_theme()` call plus a repaint.
"""
from __future__ import annotations

import math
import traceback
from collections import deque

from PySide6.QtCore import (
    Property, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QRectF,
    QSize, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import icons
from .theme import DARK, MONO, Theme

# The one palette every custom widget paints from.
_THEME: Theme = DARK


def set_theme(t: Theme):
    global _THEME
    _THEME = t
    icons.clear_cache()


def theme() -> Theme:
    return _THEME


def blend(fg: QColor, bg: QColor, alpha: float) -> QColor:
    """Flatten `fg` at `alpha` over `bg` into one opaque colour."""
    return QColor(
        round(bg.red() + (fg.red() - bg.red()) * alpha),
        round(bg.green() + (fg.green() - bg.green()) * alpha),
        round(bg.blue() + (fg.blue() - bg.blue()) * alpha))


# ------------------------------------------------------------------ pulse
class _Pulse(QObject):
    """One shared clock for every pulsing element, instead of a timer each."""

    tick = Signal()

    def __init__(self):
        super().__init__()
        self.phase = 0.0
        self._t = QTimer(self)
        self._t.timeout.connect(self._step)
        self._t.start(60)

    def _step(self):
        self.phase = (self.phase + 0.06) % 1.0
        self.tick.emit()

    def wave(self, lo: float = 0.35, hi: float = 1.0) -> float:
        return lo + (hi - lo) * (0.5 + 0.5 * math.sin(self.phase * 2 * math.pi))


_PULSE: _Pulse | None = None


def pulse() -> _Pulse:
    global _PULSE
    if _PULSE is None:
        _PULSE = _Pulse()
    return _PULSE


# ------------------------------------------------------------------ effects
def fade_in(w: QWidget, ms: int = 180):
    """Fade a widget (usually a freshly-opened dialog) into view."""
    eff = QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", w)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.OutCubic)
    a.finished.connect(lambda: w.setGraphicsEffect(None))
    a.start(QPropertyAnimation.DeleteWhenStopped)
    w._fade_anim = a          # keep a reference alive for the duration
    return a


# ------------------------------------------------------------------- cards
class Card(QFrame):
    """Rounded surface panel with an optional title strip."""

    def __init__(self, title: str = "", parent=None, pad: int = 14):
        super().__init__(parent)
        self.setObjectName("Card")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(pad, pad, pad, pad)
        self.box.setSpacing(10)
        self.header = None
        if title:
            self.header = QHBoxLayout()
            self.header.setSpacing(8)
            lbl = QLabel(title.upper())
            lbl.setObjectName("SectionTitle")
            self.header.addWidget(lbl)
            self.header.addStretch()
            self.box.addLayout(self.header)

    def add(self, w):
        self.box.addWidget(w)
        return w

    def add_header_widget(self, w):
        if self.header is not None:
            self.header.addWidget(w)
        return w


# --------------------------------------------------------------- sparkline
class Sparkline(QWidget):
    """Rolling history plot: gradient fill under a smoothed line."""

    def __init__(self, points: int = 64, color: str = "accent", parent=None):
        super().__init__(parent)
        self.values: deque[float] = deque([0.0] * points, maxlen=points)
        self.color_key = color
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def push(self, v: float):
        self.values.append(max(0.0, float(v)))
        self.update()

    def reset(self):
        self.values = deque([0.0] * self.values.maxlen, maxlen=self.values.maxlen)
        self.update()

    def paintEvent(self, _):
        t = theme()
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        vals = list(self.values)
        peak = max(vals) or 1.0
        step = w / max(1, len(vals) - 1)

        pts = [(i * step, h - 2 - (v / peak) * (h - 6)) for i, v in enumerate(vals)]
        line = QPainterPath()
        line.moveTo(*pts[0])
        # Catmull-Rom-ish smoothing: midpoint quads read much better than
        # straight segments at this size.
        for i in range(1, len(pts)):
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            line.quadTo(x0 + step / 2, y0, (x0 + x1) / 2, (y0 + y1) / 2)
            line.quadTo(x1 - step / 2, y1, x1, y1)

        fill = QPainterPath(line)
        fill.lineTo(w, h)
        fill.lineTo(0, h)
        fill.closeSubpath()

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        base = t.q(self.color_key)
        grad = QLinearGradient(0, 0, 0, h)
        g0 = QColor(base)
        g0.setAlphaF(0.34)
        g1 = QColor(base)
        g1.setAlphaF(0.0)
        grad.setColorAt(0.0, g0)
        grad.setColorAt(1.0, g1)
        p.fillPath(fill, grad)
        p.setPen(QPen(base, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        # head dot
        hx, hy = pts[-1]
        p.setBrush(base)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(hx - 2.6, hy - 2.6, 5.2, 5.2))
        p.end()


# --------------------------------------------------------------- stat tile
class StatTile(QFrame):
    """Headline number that counts up to each new value, plus a trend line."""

    def __init__(self, label: str, icon_name: str, color: str = "accent",
                 suffix: str = "", spark: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.color_key = color
        self.icon_name = icon_name
        self.suffix = suffix
        self._shown = 0.0
        self._target = 0.0
        self._decimals = 0
        self._sub = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(7)
        self.ico = QLabel()
        self.ico.setFixedSize(18, 18)
        top.addWidget(self.ico)
        self.lbl = QLabel(label.upper())
        self.lbl.setObjectName("SectionTitle")
        top.addWidget(self.lbl)
        top.addStretch()
        lay.addLayout(top)

        self.value = QLabel("0")
        f = QFont()
        f.setPointSizeF(20)
        f.setWeight(QFont.DemiBold)
        self.value.setFont(f)
        lay.addWidget(self.value)

        self.sub = QLabel("")
        self.sub.setObjectName("Subtle")
        self.sub.setStyleSheet("font-size:11px;")
        lay.addWidget(self.sub)

        self.spark = Sparkline(48, color) if spark else None
        if self.spark:
            self.spark.setFixedHeight(28)
            lay.addWidget(self.spark)
        lay.addStretch()

        self.anim = QVariantAnimation(self)
        self.anim.setDuration(420)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._on_frame)
        self.setMinimumWidth(150)
        self.restyle()

    def restyle(self):
        t = theme()
        self.ico.setPixmap(icons.pixmap(self.icon_name, getattr(t, self.color_key), 18))
        self.value.setStyleSheet(f"color:{getattr(t, self.color_key)};font-size:26px;font-weight:600;")
        if self.spark:
            self.spark.update()

    def _on_frame(self, v):
        self._shown = float(v)
        self._render()

    def _render(self):
        txt = (f"{self._shown:.{self._decimals}f}" if self._decimals
               else f"{int(round(self._shown))}")
        self.value.setText(txt + self.suffix)

    def set_value(self, v: float, sub: str = "", decimals: int = 0):
        self._decimals = decimals
        if sub != self._sub:
            self._sub = sub
            self.sub.setText(sub)
        if abs(v - self._target) < 1e-9:
            return
        self.anim.stop()
        self.anim.setStartValue(self._shown)
        self.anim.setEndValue(float(v))
        self._target = float(v)
        self.anim.start()

    def push_spark(self, v: float):
        if self.spark:
            self.spark.push(v)


# ------------------------------------------------------------ toggle switch
class ToggleSwitch(QAbstractButton):
    """Animated on/off switch -- reads far better than a checkbox for faults."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 23)
        self._pos = 0.0
        self.anim = QPropertyAnimation(self, b"knob", self)
        self.anim.setDuration(160)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self.anim.stop()
        self.anim.setStartValue(self._pos)
        self.anim.setEndValue(1.0 if on else 0.0)
        self.anim.start()

    def get_knob(self) -> float:
        return self._pos

    def set_knob(self, v: float):
        self._pos = float(v)
        self.update()

    knob = Property(float, get_knob, set_knob)

    def setChecked(self, on):        # keep the knob in sync on programmatic set
        super().setChecked(on)
        self._pos = 1.0 if on else 0.0
        self.update()

    def sizeHint(self):
        return QSize(42, 23)

    def paintEvent(self, _):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        off = t.q("border")
        on = t.q("accent")
        col = QColor(
            int(off.red() + (on.red() - off.red()) * self._pos),
            int(off.green() + (on.green() - off.green()) * self._pos),
            int(off.blue() + (on.blue() - off.blue()) * self._pos))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 4
        x = r.left() + 2 + self._pos * (r.width() - d - 4)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, r.top() + 2, d, d))
        p.end()


# ------------------------------------------------------------ table delegate
STATE_COLORS = {
    "running": "ok", "error": "err", "pending": "info",
    "preparing": "info", "stopped": "text_faint", "disabled": "text_faint",
}


class RowDelegate(QStyledItemDelegate):
    """Paints the camera table: rounded row bands, status pills, fault badges.

    Column roles are read from the item's UserRole so the table stays a plain
    QTableWidget while still looking custom-built.
    """

    KIND = Qt.UserRole + 1        # 'state' | 'fault' | 'mono' | 'num' | None
    _reported = False

    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.table = table
        self.hover_row = -1
        pulse().tick.connect(self._repaint_running)

    def _repaint_running(self):
        vp = self.table.viewport()
        if vp.isVisible() and self.table.rowCount():
            vp.update()

    PAD = 12          # must match QHeaderView::section padding in theme.py

    def _cell_font(self, kind, col: int, base: QFont) -> QFont:
        f = QFont(base)
        if kind == "mono":
            f.setFamilies([s.strip() for s in MONO.split(",")])
        if col == 0:
            f.setWeight(QFont.DemiBold)
        return f

    def sizeHint(self, opt, idx):
        # Measure with the font the cell is actually painted in. Falling back
        # to the default proportional metrics under-measures every mono column,
        # and ResizeToContents then clamps it to the minimum width.
        kind = idx.data(self.KIND)
        col = idx.column()
        if kind == "actions":
            return QSize(134, 42)          # holds the row's action buttons
        text = str(idx.data(Qt.DisplayRole) or "")
        w = QFontMetrics(self._cell_font(kind, col, opt.font)).horizontalAdvance(text)
        w += self.PAD * 2 + 6
        if col in (0, self.table.columnCount() - 1):
            w += 6
        if kind == "state":
            w += 34                          # the pill's dot and its inset
        elif kind == "fault":
            w += 18
        return QSize(w, 42)

    def paint(self, p: QPainter, opt, idx):
        # Qt calls this from C++: an exception escaping here would leave the
        # painter's save/restore stack unbalanced and crash the process, so the
        # whole body is guarded.
        p.save()
        try:
            self._paint(p, opt, idx)
        except Exception:
            if not RowDelegate._reported:      # once, not once per repaint
                RowDelegate._reported = True
                traceback.print_exc()
        finally:
            p.restore()

    def _paint(self, p: QPainter, opt, idx):
        t = theme()
        p.setRenderHint(QPainter.Antialiasing, True)

        row, col = idx.row(), idx.column()
        last = self.table.columnCount() - 1
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = row == self.hover_row

        # --- row band. Integer rects filled with fillRect() so neighbouring
        # cells tile exactly; an antialiased path per cell leaves 1px seams
        # straight through the row.
        box = opt.rect.adjusted(0, 3, 0, -3)
        if col == 0:
            box.adjust(6, 0, 0, 0)
        elif col == last:
            box.adjust(0, 0, -6, 0)
        # Opaque bands only: a translucent fill double-blends wherever two
        # cells meet and leaves a bright seam down the row.
        base = t.q("surface")
        if selected:
            bg = blend(t.q("accent"), base, 0.18)
        elif hovered:
            bg = t.q("surface_alt")
        elif row % 2:
            bg = blend(t.q("surface_alt"), base, 0.45)
        else:
            bg = QColor(Qt.transparent)
        rad = 10
        if bg.alpha():
            if col in (0, last) and box.width() > rad * 2:
                path = QPainterPath()
                path.addRoundedRect(QRectF(box), rad, rad)
                p.fillPath(path, bg)
                if col == 0:                       # square the joining edge
                    p.fillRect(QRect(box.right() - rad, box.top(),
                                     rad + 1, box.height()), bg)
                else:
                    p.fillRect(QRect(box.left(), box.top(),
                                     rad, box.height()), bg)
            else:
                p.fillRect(box, bg)
        if selected and col == 0:
            p.fillRect(QRect(box.left(), box.top() + 3, 3, box.height() - 6),
                       t.q("accent"))

        kind = idx.data(self.KIND)
        text = idx.data(Qt.DisplayRole) or ""
        pad = self.PAD
        inner = QRectF(opt.rect).adjusted(pad + (6 if col == 0 else 0), 0,
                                          -(pad + (6 if col == last else 0)), 0)

        if kind == "state":
            self._pill(p, inner, str(text))
        elif kind == "fault":
            if str(text).strip():
                self._badge(p, inner, str(text), "warn")
        else:
            f = self._cell_font(kind, col, p.font())
            p.setFont(f)
            fg = t.q("text") if col == 0 else t.q("text_dim")
            if kind == "error" and str(text).strip():
                fg = t.q("err")
            p.setPen(fg)
            align = int(Qt.AlignVCenter | (Qt.AlignRight if kind == "num"
                                           else Qt.AlignLeft))
            elided = QFontMetrics(f).elidedText(str(text), Qt.ElideRight,
                                                int(inner.width()))
            p.drawText(inner, align, elided)

    def _pill(self, p: QPainter, rect: QRectF, state: str):
        t = theme()
        key = STATE_COLORS.get(state, "text_faint")
        col = t.q(key)
        f = QFont(p.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(state)
        h = 22.0
        w = tw + 32
        box = QRectF(rect.left(), rect.center().y() - h / 2, w, h)
        bgc = QColor(col)
        bgc.setAlphaF(0.15)
        p.setPen(Qt.NoPen)
        p.setBrush(bgc)
        p.drawRoundedRect(box, h / 2, h / 2)
        # a live camera gets a breathing dot; everything else a steady one
        cy = box.center().y()
        cx = box.left() + 13
        if state == "running":
            g = pulse().wave(0.15, 0.55)
            halo = QColor(col)
            halo.setAlphaF(g)
            p.setBrush(halo)
            rr = 4.0 + 3.0 * g
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))
        p.setBrush(col)
        p.drawEllipse(QRectF(cx - 3.2, cy - 3.2, 6.4, 6.4))
        p.setPen(col)
        p.drawText(QRectF(box.left() + 23, box.top(), box.width() - 26, h),
                   int(Qt.AlignVCenter | Qt.AlignLeft), state)

    def _badge(self, p: QPainter, rect: QRectF, text: str, key: str):
        t = theme()
        col = t.q(key)
        f = QFont(p.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1.5))
        f.setWeight(QFont.Bold)
        p.setFont(f)
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 18
        h = 20.0
        box = QRectF(rect.left(), rect.center().y() - h / 2, w, h)
        bgc = QColor(col)
        bgc.setAlphaF(0.18)
        p.setPen(QPen(QColor(col), 1))
        p.setBrush(bgc)
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(col)
        p.drawText(box, int(Qt.AlignCenter), text)


# --------------------------------------------------------------- busy overlay
class BusyOverlay(QWidget):
    """Full-panel scrim with a rotating arc, a message and optional progress."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.angle = 0.0
        self.message = "Working..."
        self.detail = ""
        self.frac = -1.0                 # <0 -> indeterminate
        self._t = QTimer(self)
        self._t.timeout.connect(self._spin)
        self.hide()

    def _spin(self):
        self.angle = (self.angle + 6.0) % 360.0
        self.update()

    def start(self, message: str = "Working...", detail: str = ""):
        self.message, self.detail = message, detail
        self.resize(self.parentWidget().size())
        self.raise_()
        self.show()
        self._t.start(16)
        fade_in(self, 150)

    def update_text(self, message: str = None, detail: str = None,
                    frac: float = None):
        if message is not None:
            self.message = message
        if detail is not None:
            self.detail = detail
        if frac is not None:
            self.frac = frac
        self.update()

    def stop(self):
        self._t.stop()
        self.frac = -1.0
        self.hide()

    def paintEvent(self, _):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), t.scrim())
        cx, cy = self.width() / 2, self.height() / 2 - 24
        rad = 22.0
        ring = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
        p.setPen(QPen(t.q("border"), 3.5))
        p.drawArc(ring, 0, 360 * 16)
        p.setPen(QPen(t.q("accent"), 3.5, Qt.SolidLine, Qt.RoundCap))
        if self.frac >= 0:
            p.drawArc(ring, 90 * 16, -int(360 * 16 * min(1.0, self.frac)))
        else:
            p.drawArc(ring, int(-self.angle * 16), 110 * 16)

        f = QFont()
        f.setPointSizeF(11.5)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(t.q("text"))
        p.drawText(QRect(0, int(cy + rad + 16), self.width(), 24),
                   int(Qt.AlignHCenter | Qt.AlignTop), self.message)
        if self.detail:
            f2 = QFont()
            f2.setPointSizeF(9.5)
            p.setFont(f2)
            p.setPen(t.q("text_dim"))
            p.drawText(QRect(0, int(cy + rad + 40), self.width(), 22),
                       int(Qt.AlignHCenter | Qt.AlignTop), self.detail)
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)


# --------------------------------------------------------------------- toast
class Toast(QFrame):
    """Slide-in notification; stacks upward from the bottom-right corner."""

    _live: list["Toast"] = []
    H = 46
    GAP = 8

    def __init__(self, parent: QWidget, text: str, kind: str = "info",
                 ms: int = 3200):
        super().__init__(parent)
        t = theme()
        col = {"ok": t.ok, "err": t.err, "warn": t.warn}.get(kind, t.accent)
        self.setObjectName("ToastFrame")
        self.setStyleSheet(
            f"#ToastFrame{{background:{t.elevated};border:1px solid {t.border};"
            f"border-left:3px solid {col};border-radius:10px;}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 14, 0)
        lay.setSpacing(9)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(
            {"ok": "activity", "err": "alert", "warn": "alert"}.get(kind, "bell"),
            col, 16))
        lay.addWidget(ic)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{t.text};font-weight:600;")
        lay.addWidget(lbl)
        lay.addStretch()
        # resize before positioning -- _target_pos reads width(), and an
        # unsized toast lands half off the right edge
        self.resize(min(420, max(190, lbl.sizeHint().width() + 78)), self.H)

        self.eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.eff)
        self.eff.setOpacity(0.0)

        Toast._live.append(self)
        self._place(first=True)
        self.show()
        self.raise_()

        self.fade = QPropertyAnimation(self.eff, b"opacity", self)
        self.fade.setDuration(180)
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.start()
        QTimer.singleShot(ms, self.dismiss)

    def _target_pos(self, index: int) -> QPoint:
        p = self.parentWidget()
        x = p.width() - self.width() - 22
        y = p.height() - (index + 1) * (self.H + self.GAP) - 16
        return QPoint(x, y)

    def _place(self, first: bool = False):
        idx = Toast._live.index(self) if self in Toast._live else 0
        end = self._target_pos(idx)
        if first:
            self.move(end + QPoint(0, 26))
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(240)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setEndValue(end)
        a.start(QPropertyAnimation.DeleteWhenStopped)
        self._slide = a

    def dismiss(self):
        if self not in Toast._live:
            return
        Toast._live.remove(self)
        a = QPropertyAnimation(self.eff, b"opacity", self)
        a.setDuration(170)
        a.setEndValue(0.0)
        a.finished.connect(self.deleteLater)
        a.start()
        self._out = a
        for other in Toast._live:
            other._place()


def toast(parent: QWidget, text: str, kind: str = "info", ms: int = 3200):
    while len(Toast._live) >= 4:
        Toast._live[0].dismiss()
    return Toast(parent, text, kind, ms)
