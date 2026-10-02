"""Assemble application binaries and notices; FFmpeg is installed separately.

Run: python tools/make_portable.py [--zip]
Review docs/release.md before distributing any executable build.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil

try:
    from .export_source import export_source
except ImportError:
    from export_source import export_source

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUT = DIST / "VCamSim-portable"


def collect_notices(destination: Path):
    inventory = []
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
                 "PyYAML", "pywin32"):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        inventory.append({"name": name, "version": dist.version})
        for entry in dist.files or []:
            if not any("license" in p.lower() or "copying" in p.lower()
                       for p in entry.parts):
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file():
                target = destination / "licenses" / name / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                # Avoid overwriting distinct notices with the same basename.
                if target.exists() and target.read_bytes() != source.read_bytes():
                    target = target.with_name("_".join(p for p in entry.parts
                                                    if p not in ("..", ".")))
                shutil.copy2(source, target)
    (destination / "dependencies.json").write_text(
        json.dumps(inventory, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    exe = DIST / "VCamSim.exe"
    if not exe.is_file():
        raise SystemExit("Build dist/VCamSim.exe first using VCamSim.spec.")
    expected = ROOT.resolve() / "dist" / "VCamSim-portable"
    if OUT.is_symlink() or OUT.resolve() != expected:
        raise SystemExit("Refusing to replace an output directory outside this project.")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copy2(exe, OUT / exe.name)
    svc = DIST / "VCamSimSvc.exe"
    if svc.is_file():
        shutil.copy2(svc, OUT / svc.name)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "config.example.yaml"):
        shutil.copy2(ROOT / name, OUT / name)
    shutil.copytree(ROOT / "licenses", OUT / "licenses", dirs_exist_ok=True)
    shutil.copy2(ROOT / "docs" / "release.md", OUT / "RELEASE.md")
    collect_notices(OUT)
    export_source(OUT / "VCamSim-source.zip")
    print(f"Assembled {OUT}")
    print("FFmpeg and ffprobe must be installed separately on the target PC.")
    print("Before publication, complete the Qt/dependency checks in RELEASE.md.")
    if args.zip:
        path = shutil.make_archive(str(DIST / "VCamSim-portable"), "zip",
                                   root_dir=DIST, base_dir=OUT.name)
        print(f"Archive: {path}")


if __name__ == "__main__":
    main()
