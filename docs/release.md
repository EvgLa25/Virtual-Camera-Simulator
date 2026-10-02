# Build and publish

## Public source release

```powershell
python -m pytest -q
python -m ruff check vcamsim tests tools
python tools/export_source.py --output dist/VCamSim-source.zip
```

The exporter includes application code, tests, documentation, license notices
and build recipes. It excludes configuration, tokens, videos, caches, private
notes, old binaries, logs and local environments. Review both the resulting
archive and `git status` before publishing. Do not upload a ZIP of the entire
development directory.

Initialize a GitHub repository using the reviewed source files. Enable Actions
to run the regression suite and static checks on subsequent changes. A fresh
clone must contain no real camera credentials or local control token.

## Local Windows executables

```powershell
python -m pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean VCamSim.spec
python -m PyInstaller --noconfirm --clean VCamSimSvc.spec
python tools/make_portable.py --zip
```

The portable assembler copies application executables, source, notices and
dependency license files. It does **not** copy FFmpeg/ffprobe. Install these
separately on the target machine. `build_exe.bat` and `build_installer.bat`
provide shortcuts; Inno Setup 6 is additionally needed for the installer.

Building successfully does not clear an executable for public redistribution.
Complete the Qt/PySide source/replacement and third-party obligations described
in THIRD_PARTY_NOTICES.md for the exact bundled versions before publishing
binary artifacts. Keep versioned dependency records and corresponding source
access with that release. Source releases do not contain these binaries.

## Optional service

Install pywin32 for a source-based service, or build `VCamSimSvc.exe`.
From an elevated terminal beside the service executable:

```powershell
.\VCamSimSvc.exe install
.\VCamSimSvc.exe start
```

The service reads `config.yaml` beside itself. The GUI automatically attaches
to an installed running service. `VCAMSIM_NO_SERVICE=1` forces local mode for
tests. The control port is fixed while the service runs; changing it requires
a service restart. Preserve `config.yaml` during upgrades.
