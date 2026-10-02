# Third-party components

The MIT license in `LICENSE` covers VCamSim's original code. It does not
relicense dependencies, icons, codecs, sample videos, or third-party binaries.

| Component | Use | License / primary source |
| --- | --- | --- |
| PySide6, Shiboken6, Qt | Desktop interface; installed separately for a source checkout | [LGPLv3 / GPL / commercial options](https://doc.qt.io/qtforpython-6/) and [Qt module licenses](https://doc.qt.io/qt-6/licensing.html) |
| PyYAML | YAML configuration | [MIT](https://github.com/yaml/pyyaml/blob/main/LICENSE); copy in `licenses/PyYAML.txt` |
| Lucide / Feather | Inline SVG icon adaptations in `vcamsim/icons.py` | [ISC and MIT](https://lucide.dev/license); both notices in `licenses/Lucide-Feather.txt` |
| pywin32 | Optional Windows service host | [Project license files](https://github.com/mhammond/pywin32); retain the license files from the exact installed distribution when packaging |
| FFmpeg, ffprobe | Separate command-line programs for import and inspection | [LGPL/GPL depending on build configuration](https://ffmpeg.org/legal.html); not included in the source release or copied by the default packaging script |
| PyInstaller | Optional executable builder | [GPL with bootloader exception](https://pyinstaller.org/en/stable/license.html); the exception does not waive dependency obligations |

No font files are shipped. The GUI uses fonts installed on the user's system.
The tests generate their own synthetic video using FFmpeg. Users must supply
video they have permission to use and redistribute.

## Executable distribution

Source publication and binary distribution have different obligations.
The historical portable package included FFmpeg 8.0.1 from gyan.dev built with
`--enable-gpl --enable-version3 --enable-libx264 --enable-libx265`. That build
is GPL-enabled, not an LGPL-only build. Historical executables, installers,
cached videos and private configuration are excluded from the public source.

Before distributing new Qt/PySide executables, provide the applicable license
texts, required notices, corresponding source access, and a way to replace or
rebuild the LGPL components. Audit the exact modules, plugins and third-party
libraries actually bundled; PyInstaller's exception alone is not sufficient.
The complete application source and build instructions must accompany the
release or be available at a stable release-specific location.

If you choose to distribute FFmpeg in a later release, satisfy the license of
that exact build, including corresponding source for the enabled components.
A generic FFmpeg homepage link is not a substitute for matching source.
Do not redistribute a build marked `--enable-nonfree` without resolving its
restrictions. Codec patents and trademark rights are separate from copyright.

This is an engineering inventory based on the checked source and local
binaries, not a legal opinion or a guarantee about the provenance of every
line. Review the upstream terms for the exact versions you distribute.
