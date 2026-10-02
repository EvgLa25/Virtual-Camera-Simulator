# PyInstaller spec -- single-file Windows build.
# Build with:  pyinstaller --noconfirm --clean VCamSim.spec
#
# ffmpeg is NOT embedded (the static build is ~200 MB). The exe looks for
# ffmpeg.exe next to itself, then in .\ffmpeg\ or .\ffmpeg\bin\, then on PATH.
# tools\make_portable.py includes notices and source; install FFmpeg separately.

EXCLUDES = [
    # Qt modules we never touch -- keeps the binary far smaller
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNfc",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    # NOTE: QtSvg must NOT be excluded -- vcamsim/icons.py renders the whole
    # icon set through QSvgRenderer, so dropping it breaks the exe at startup.
    "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets", "PySide6.QtXml",
    # scientific / dev stacks that sometimes get pulled in
    "numpy", "scipy", "pandas", "matplotlib", "PIL", "tkinter",
    "pytest", "IPython", "setuptools", "pip",
]

a = Analysis(
    ["run_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[("config.example.yaml", "."), ("README.md", "."),
           ("LICENSE", "."), ("THIRD_PARTY_NOTICES.md", "."), ("licenses", "licenses")],
    hiddenimports=["vcamsim.gui", "vcamsim.cli", "vcamsim.engine",
                   "vcamsim.theme", "vcamsim.widgets", "vcamsim.icons",
                   "vcamsim.proxy", "vcamsim.control", "vcamsim.svcctl",
                   "vcamsim.daemon",
                   "PySide6.QtSvg"],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VCamSim",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # GUI app -- no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
