import zipfile

from tools.export_source import export_source


def test_export_excludes_private_and_generated_files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "config.yaml", "control.token"):
        (root / name).write_text("fixture")
    for directory in ("vcamsim", "media_cache", "Vault", "dist"):
        (root / directory).mkdir()
    (root / "vcamsim" / "config.py").write_text("# public")
    (root / "media_cache" / "private.mp4").write_bytes(b"private")
    (root / "Vault" / "notes.md").write_text("private")
    (root / "dist" / "app.exe").write_bytes(b"private")
    output = tmp_path / "release.zip"
    assert export_source(output, root) == 4
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert "VCamSim/vcamsim/config.py" in names
    assert not any(part in name for name in names for part in ("config.yaml", "token", "private", "Vault", "dist"))
