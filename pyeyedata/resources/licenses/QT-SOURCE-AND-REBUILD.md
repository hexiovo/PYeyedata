# Qt/PySide/Shiboken source and rebuild information

PYeyedata uses the LGPLv3 option for the Qt, PySide and Shiboken components selected for its Windows build, including Qt Base, QtSvg and Qt Image Formats. The full LGPLv3 and GPLv3 texts accompany this document. These instructions preserve the reverse-engineering/debugging permission needed to diagnose modifications to the covered LGPL components; they do not license PYeyedata source code under LGPL or GPL.

## Matching upstream source archives

The source archive references below are for release 6.8.3. Before using an archive, verify its SHA-256 against the official Qt sidecar. `source-manifest.json` records the URLs and verification evidence. The sidecars were fetched and checked; none of the Qt source archives was downloaded for a local archive-hash check.

- PySide/Shiboken source: [Qt for Python 6.8.3 source archive directory](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.8.3-src/), archive `pyside-setup-everywhere-src-6.8.3.tar.xz`. The official `.sha256` sidecar reports `d2c896f7f1a6a7ea4c9f98cfe317272fd139703ddf612b86999f09e48aeef0e6`.
- Qt Base source, including Qt Core, Gui, Widgets and Network: [Qt 6.8.3 source archive](https://download.qt.io/archive/qt/6.8/6.8.3/submodules/qtbase-everywhere-src-6.8.3.zip). The official `.sha256` sidecar reports `992bf7766e214a341ef793eb3665fb784787d2fd666955f5f507f4c6f1f770dd`.
- QtSvg source: [Qt 6.8.3 source archive](https://download.qt.io/archive/qt/6.8/6.8.3/submodules/qtsvg-everywhere-src-6.8.3.tar.xz). The official `.sha256` sidecar reports `35eb516460f00f264eb504baa253432384351cf23fb9980a5857190e8deef438`.
- Qt Image Formats source: [Qt 6.8.3 source archive](https://download.qt.io/archive/qt/6.8/6.8.3/submodules/qtimageformats-everywhere-src-6.8.3.tar.xz). The official `.sha256` sidecar reports `049bfb99845e4801672aca07c3c4fc4c140f932a3a33faa899419579e33ef1c8`.
- Qt for Python licensing details: [Qt for Python 6.8 licensing](https://doc.qt.io/qtforpython-6.8/licenses.html). Qt licensing details: [Qt 6.8 licensing](https://doc.qt.io/qt-6.8/licensing.html).

The matching Qt 6.8.3 attribution JSON and applicable license/notice texts for Qt Base and Qt Image Formats are provided under `qt/6.8.3/`. Their exact upstream paths, source URLs, digests and module mappings are recorded in `source-manifest.json`. These materials cover the bundled Qt Base dependencies identified by Qt's own attribution records (double-conversion, FreeType and its full FTL, BDF, PCF and zlib notices, HarfBuzz, libjpeg, libpng, PCRE2/SLJIT and zlib) and the Qt Image Formats libtiff/libwebp notices, including the WebP patent notice. The official JSON attribution records are hash-checked but are not rendered as license text by the offline viewer.

The release builder reconciles every actual native Analysis path and binary hash against the maintained 281-entry source map, including QtSvg and Qt Image Formats, and verifies the indexed notice hashes before packaging. The resulting native-components.json accompanies the portable release; source-manifest.json provides package versions, source archive references and notice mappings. If a selected component changes, its exact corresponding source and notice map must be updated before the release check can pass.

## Replacing Qt/PySide/Shiboken components and rebuilding

1. Obtain the exact source archive identified for the component in the frozen component manifest and verify its SHA-256. Use the corresponding license notice in this directory.
2. Build the library for the same Windows architecture, Python ABI, Qt version and release configuration as the bundled component. Build PySide/Shiboken from the matching 6.8.3 source and the application’s Python 3.12 ABI. Preserve the compatible library interfaces and dependencies required by the application.
3. Use the release PyInstaller specification and pinned requirements to rebuild the one-file executable with the replacement shared libraries. The D02 build report records that specification, toolchain and component mapping; do not rely on the original binary's temporary extraction directory as a permanent replacement location.
4. Run the rebuilt executable and compare its native component inventory with the build manifest. Keep the complete LGPL/GPL texts, source references and this replacement guidance with the resulting executable.

The release must not impose a term that removes the LGPLv3 permission to reverse engineer for debugging modifications to the covered library. No source license is asserted here for PYeyedata itself.

