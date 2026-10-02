# Licensing review — 2026-10-02

The source tree was inspected for imports, notices, inline artwork and bundled
assets. The dependency licenses are documented in THIRD_PARTY_NOTICES.md.
MIT covers original project code; Lucide/Feather notices cover icon adaptations.

The old distribution contained a GPLv3-enabled FFmpeg executable and bundled
Qt/PySide binaries without a complete redistribution package. Those artifacts
are excluded from the public source. The default build assembler now leaves
FFmpeg installation to the user and includes notices and application source.
Public binary releases still require the release-specific review described in
docs/release.md; no blanket approval of the old installer is implied.

The review cannot establish ownership of code copied from unknown sources or
rights to the operator's videos. No operator video, configuration, token or
private development notes are included in the source export.

Primary sources:
- https://doc.qt.io/qtforpython-6/
- https://doc.qt.io/qt-6/licensing.html
- https://ffmpeg.org/legal.html
- https://pyinstaller.org/en/stable/license.html
- https://github.com/yaml/pyyaml/blob/main/LICENSE
- https://github.com/mhammond/pywin32
- https://lucide.dev/license
