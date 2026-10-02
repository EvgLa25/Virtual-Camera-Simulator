"""Export an explicit public source set, never the entire working directory."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = (
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md",
    "CONTRIBUTING.md", "CHANGELOG.md", ".gitignore", ".gitattributes",
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "requirements-tested.txt", "config.example.yaml", "run_gui.py", "run_service.py",
    "VCamSim.spec", "VCamSimSvc.spec", "build_exe.bat", "build_installer.bat",
)
PATTERNS = (
    "vcamsim/*.py", "tools/*.py", "tests/*.py", "docs/*.md",
    "docs/images/dashboard.png", "docs/images/empty-state.png",
    "docs/images/social-preview.jpg",
    ".github/ISSUE_TEMPLATE/*.yml", ".github/PULL_REQUEST_TEMPLATE.md",
    "licenses/*.txt", ".github/workflows/*.yml", "installer/*.iss",
)


def source_files(root: Path = ROOT) -> list[Path]:
    root = root.resolve()
    paths = {root / name for name in TOP_LEVEL if (root / name).is_file()}
    for pattern in PATTERNS:
        paths.update(p for p in root.glob(pattern) if p.is_file())
    for p in paths:
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError(f"Public source cannot include links outside the project: {p.name}")
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def export_source(output: Path, root: Path = ROOT):
    files = source_files(root)
    for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"):
        if root / name not in files:
            raise ValueError(f"Missing required release file: {name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "VCamSim/" + path.relative_to(root).as_posix())
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "VCamSim-source.zip")
    args = parser.parse_args()
    count = export_source(args.output.resolve())
    print(f"Exported {count} public source files to {args.output}")


if __name__ == "__main__":
    main()
