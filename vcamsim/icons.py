"""Vector icons rendered from inline SVG -- no asset files, no dependencies.

Icon paths adapt Lucide / Feather designs; see licenses/Lucide-Feather.txt.
Every icon uses a 24x24 stroked path. `icon(name, colour)`
renders one into a cached QIcon, so re-theming is just asking for the same name
in a different colour.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PATHS: dict[str, str] = {
    "play": "M6 4.5 20 12 6 19.5z",
    "stop": "M6.5 6.5h11v11h-11z",
    "plus": "M12 5v14M5 12h14",
    "grid": "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
    "edit": "M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3zM14.5 6.5l3 3",
    "trash": "M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6",
    "alert": "M12 3 2 20h20L12 3zM12 9v5M12 17.5v.5",
    "activity": "M2 12h4l3 8 6-16 3 8h4",
    "copy": "M9 9h11v11H9zM4 15V4h11",
    "shield": "M12 3 4 6v6c0 5 3.5 8.2 8 9 4.5-.8 8-4 8-9V6l-8-3z",
    "settings": "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z"
                "M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.3a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3.5 15H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 8.2l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 10 4.5V4a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.3a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.4 1z",
    "save": "M5 4h11l3 3v13H5zM8 4v6h7V4M8 20v-6h8v6",
    "sun": "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v3M12 20v3M4.2 4.2l2.1 2.1"
           "M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
    "moon": "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z",
    "search": "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
    "camera": "M3 8h3l2-3h8l2 3h3v12H3zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    "pause": "M8 5v14M16 5v14",
    "x": "M6 6l12 12M18 6L6 18",
    "refresh": "M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5",
    "link": "M9.5 14.5 14.5 9.5M10 6l1.7-1.7a4 4 0 0 1 5.7 5.7L15.7 11"
            "M8.3 13l-1.7 1.7a4 4 0 0 0 5.7 5.7L14 18.7",
    "waves": "M2 8c2.5-2 4.5 2 7 0s4.5-2 7 0 4.5 2 6 0M2 14c2.5-2 4.5 2 7 0"
             "s4.5-2 7 0 4.5 2 6 0",
    "chip": "M7 7h10v10H7zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3",
    "eye": "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "users": "M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8M21 20v-2a4 4 0 0 0-3-3.9",
    "bell": "M18 9a6 6 0 0 0-12 0c0 6-3 7-3 7h18s-3-1-3-7M10.5 20a1.8 1.8 0 0 0 3 0",
    "folder": "M3 6h6l2 2h10v11H3zM3 6v13",
    "chev_up": "M5 15l7-7 7 7",
    "chev_down": "M5 9l7 7 7-7",
}

FILLED = {"play", "stop"}
_CACHE: dict[tuple, QIcon] = {}


def svg(name: str, color: str, width: float = 1.9) -> bytes:
    d = PATHS.get(name, PATHS["camera"])
    if name.startswith("chev_"):
        width = 2.8            # chevrons are drawn tiny; thin strokes vanish
    if name in FILLED:
        body = f'<path d="{d}" fill="{color}" stroke="{color}" ' \
               f'stroke-width="{width}" stroke-linejoin="round"/>'
    else:
        body = f'<path d="{d}" fill="none" stroke="{color}" ' \
               f'stroke-width="{width}" stroke-linecap="round" ' \
               f'stroke-linejoin="round"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            f'{body}</svg>').encode()


def pixmap(name: str, color: str, size: int = 18, dpr: float = 2.0) -> QPixmap:
    r = QSvgRenderer(QByteArray(svg(name, color)))
    px = QPixmap(QSize(int(size * dpr), int(size * dpr)))
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    r.render(p)
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


def icon(name: str, color: str, size: int = 18) -> QIcon:
    key = (name, color, size)
    hit = _CACHE.get(key)
    if hit is None:
        hit = QIcon(pixmap(name, color, size))
        _CACHE[key] = hit
    return hit


def clear_cache():
    _CACHE.clear()


# --------------------------------------------------------------- QSS arrows
# Qt ignores CSS border-triangles for sub-control arrows and wants a real
# image, so spin-box / combo-box arrows are rendered to PNGs once per
# (name, colour) and referenced from the stylesheet by path.
_ARROW_DIR = Path(tempfile.gettempdir()) / "vcamsim-ui"


def arrow_url(name: str, color: str, size: int = 10) -> str:
    try:
        _ARROW_DIR.mkdir(parents=True, exist_ok=True)
        p = _ARROW_DIR / f"{name}_{color.lstrip('#')}_{size}.png"
        if not p.exists() or p.stat().st_size == 0:
            pixmap(name, color, size, dpr=1.0).save(str(p), "PNG")
        return p.as_posix()          # QSS url() needs forward slashes
    except Exception:
        # an unwritable temp dir must not take the whole stylesheet down;
        # the spin-box arrows simply render blank
        return ""
