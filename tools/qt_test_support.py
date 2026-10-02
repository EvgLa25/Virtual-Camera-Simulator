"""Load system fonts when Windows' offscreen Qt plugin has no font database."""
import os
import sys
from pathlib import Path


def load_test_fonts():
    if sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return
    from PySide6.QtGui import QFontDatabase
    folder = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("segoeui.ttf", "segoeuib.ttf", "consola.ttf"):
        path = folder / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
