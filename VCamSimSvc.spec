# PyInstaller spec -- the Windows service host (VCamSimSvc.exe).
# Build with:  pyinstaller --noconfirm --clean VCamSimSvc.spec
#
# Console binary on purpose: a pywin32 service host cannot be a windowed exe.
# No Qt at all -- the service has no desktop -- which keeps this ~15 MB against
# the GUI's ~50 MB.

EXCLUDES = [
    "PySide6", "shiboken6", "PySide6.QtCore", "PySide6.QtGui",
    "PySide6.QtWidgets", "PySide6.QtSvg",
    "numpy", "scipy", "pandas", "matplotlib", "PIL", "tkinter",
    "pytest", "IPython", "setuptools", "pip",
]

a = Analysis(
    ["run_service.py"],
    pathex=["."],
    binaries=[],
    datas=[("LICENSE", "."), ("THIRD_PARTY_NOTICES.md", "."), ("licenses", "licenses")],
    hiddenimports=[
        "vcamsim.daemon", "vcamsim.control", "vcamsim.engine",
        "vcamsim.service",
        # pywin32 service plumbing -- PyInstaller cannot see these statically,
        # and win32timezone is imported lazily by the service framework
        "win32serviceutil", "win32service", "win32event", "servicemanager",
        "win32timezone",
    ],
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
    name="VCamSimSvc",
    debug=False,
    strip=False,
    upx=False,
    console=True,           # required: services run without a desktop
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
