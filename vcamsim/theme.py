"""Design tokens + the application stylesheet.

One place decides every colour, radius and font in the UI. `Theme.DARK` and
`Theme.LIGHT` are the two palettes; `stylesheet(theme)` turns one into the Qt
style sheet applied to the whole QApplication, and the custom-painted widgets
(sparklines, pills, spinners) read the same tokens so nothing drifts.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str
    surface_alt: str
    elevated: str
    border: str
    border_soft: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hi: str
    accent_dim: str
    ok: str
    warn: str
    err: str
    info: str
    purple: str
    overlay: str            # scrim base; alpha comes from OVERLAY_ALPHA

    def q(self, key: str, alpha: float = 1.0) -> QColor:
        # Every token is plain #RRGGBB on purpose: Qt reads a 9-digit hex as
        # #AARRGGBB, so an "#RRGGBBAA" token silently comes out transparent.
        c = QColor(getattr(self, key))
        if alpha < 1.0:
            c.setAlphaF(alpha)
        return c

    def scrim(self) -> QColor:
        return self.q("overlay", OVERLAY_ALPHA)


OVERLAY_ALPHA = 0.86


DARK = Theme(
    name="dark",
    bg="#0e1014", surface="#161a21", surface_alt="#1c212a", elevated="#212734",
    border="#2a3140", border_soft="#20262f",
    text="#e8ecf4", text_dim="#98a2b8", text_faint="#6b7688",
    accent="#4f8cff", accent_hi="#7aa8ff", accent_dim="#2b4a8f",
    ok="#34d399", warn="#fbbf24", err="#f87171", info="#38bdf8",
    purple="#a78bfa", overlay="#0b0d11",
)

LIGHT = Theme(
    name="light",
    bg="#eef1f6", surface="#ffffff", surface_alt="#f5f7fa", elevated="#ffffff",
    border="#d8dee8", border_soft="#e6eaf1",
    text="#111725", text_dim="#5b6678", text_faint="#8b95a6",
    accent="#2563eb", accent_hi="#1d4ed8", accent_dim="#bfd3fb",
    ok="#059669", warn="#b45309", err="#dc2626", info="#0284c7",
    purple="#7c3aed", overlay="#f4f6fa",
)

THEMES = {"dark": DARK, "light": LIGHT}

MONO = "Cascadia Mono, Consolas, DejaVu Sans Mono, monospace"
SANS = "Segoe UI Variable Display, Segoe UI, Inter, system-ui, sans-serif"


def stylesheet(t: Theme) -> str:
    from . import icons
    up = icons.arrow_url("chev_up", t.text_dim, 10)
    down = icons.arrow_url("chev_down", t.text_dim, 10)
    down_lg = icons.arrow_url("chev_down", t.text_dim, 13)
    return f"""
* {{
    font-family: {SANS};
    font-size: 13px;
    outline: none;
}}
QWidget {{ color: {t.text}; background: transparent; }}
QMainWindow, QDialog {{ background: {t.bg}; }}

/* ---------------------------------------------------------- containers */
#Card {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: 14px;
}}
#Header {{
    background: {t.surface};
    border-bottom: 1px solid {t.border};
}}
#Wordmark {{ font-size: 16px; font-weight: 700; letter-spacing: 0.2px; }}
#Subtle {{ color: {t.text_dim}; }}
#SectionTitle {{
    color: {t.text_dim};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
}}
#Divider {{ background: {t.border}; max-width: 1px; min-width: 1px; border: none; }}
#HDivider {{ background: {t.border}; max-height: 1px; min-height: 1px; border: none; }}

/* ------------------------------------------------------------- buttons */
QPushButton, QToolButton {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 9px;
    padding: 7px 14px;
    color: {t.text};
    font-weight: 600;
}}
QPushButton:hover, QToolButton:hover {{
    background: {t.elevated};
    border-color: {t.accent_dim};
}}
QPushButton:pressed, QToolButton:pressed {{ background: {t.border_soft}; }}
QPushButton:disabled, QToolButton:disabled {{
    color: {t.text_faint};
    background: {t.surface};
    border-color: {t.border_soft};
}}
QPushButton[kind="primary"] {{
    background: {t.accent};
    border-color: {t.accent};
    color: #ffffff;
}}
QPushButton[kind="primary"]:hover {{ background: {t.accent_hi}; border-color: {t.accent_hi}; }}
QPushButton[kind="primary"]:disabled {{
    background: {t.accent_dim}; border-color: {t.accent_dim}; color: {t.surface};
}}
QPushButton[kind="danger"] {{ color: {t.err}; }}
QPushButton[kind="danger"]:hover {{ border-color: {t.err}; background: {t.elevated}; }}
QPushButton[kind="ghost"] {{
    background: transparent; border-color: transparent; color: {t.text_dim};
}}
QPushButton[kind="ghost"]:hover {{ background: {t.surface_alt}; color: {t.text}; }}
QPushButton[kind="ghost"]:checked {{
    background: {t.accent_dim}; color: {t.text}; border-color: {t.accent};
}}

/* -------------------------------------------------------------- inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {t.bg};
    border: 1px solid {t.border};
    border-radius: 9px;
    padding: 6px 10px;
    selection-background-color: {t.accent};
    selection-color: #ffffff;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus {{ border-color: {t.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t.text_faint}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: url({down_lg}); width: 13px; height: 13px; margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {t.elevated};
    border: 1px solid {t.border};
    border-radius: 10px;
    padding: 4px;
    selection-background-color: {t.accent};
    selection-color: #ffffff;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    background: {t.surface_alt}; border: none;
    width: 18px; margin: 1px 1px 0 0;
    border-top-right-radius: 8px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    background: {t.surface_alt}; border: none;
    width: 18px; margin: 0 1px 1px 0;
    border-bottom-right-radius: 8px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {t.accent_dim};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({up}); width: 10px; height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({down}); width: 10px; height: 10px;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1px solid {t.border};
    border-radius: 5px;
    background: {t.bg};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}

/* -------------------------------------------------------------- tables */
QTableView, QTableWidget {{
    background: transparent;
    border: none;
    gridline-color: transparent;
    selection-background-color: transparent;
    alternate-background-color: transparent;
}}
QTableView::item {{ border: none; padding: 0px; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: transparent;
    color: {t.text_faint};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 8px 12px;               /* keep in step with RowDelegate.PAD */
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* ------------------------------------------------------------ scrolling */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {t.border}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {t.border}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------------------------------------------------------- misc */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {t.accent_dim}; }}
QSplitter::handle:vertical {{ height: 7px; }}
QSplitter::handle:horizontal {{ width: 7px; }}
QMenu {{
    background: {t.elevated};
    border: 1px solid {t.border};
    border-radius: 10px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 16px; border-radius: 7px; color: {t.text};
}}
QMenu::item:selected {{ background: {t.accent}; color: #ffffff; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMenu::separator {{
    height: 1px; background: {t.border}; margin: 5px 8px;
}}
QToolTip {{
    background: {t.elevated};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 6px 9px;
}}
QGroupBox {{
    border: 1px solid {t.border};
    border-radius: 12px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {t.text_dim};
}}
QTabWidget::pane {{
    border: 1px solid {t.border}; border-radius: 12px; top: -1px;
}}
QTabBar::tab {{
    background: transparent; color: {t.text_dim};
    padding: 8px 16px; border: none; border-bottom: 2px solid transparent;
    font-weight: 600;
}}
QTabBar::tab:selected {{ color: {t.text}; border-bottom-color: {t.accent}; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QMessageBox {{ background: {t.surface}; }}
QLabel#Mono, QPlainTextEdit#Log {{ font-family: {MONO}; font-size: 12px; }}
QPlainTextEdit#Log {{
    background: {t.bg}; border: 1px solid {t.border}; border-radius: 12px;
    padding: 8px;
}}
"""
