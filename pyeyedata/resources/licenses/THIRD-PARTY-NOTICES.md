# PYeyedata 0.9.0 third-party notices

This notice file identifies upstream software and font material relevant to the release. It does not grant a new license to PYeyedata source code.

## Qt, PySide6 and Shiboken6

The locked runtime uses PySide6_Essentials 6.8.3 and shiboken6 6.8.3, with Qt Base, QtSvg and Qt Image Formats 6.8.3 native components. The selected open-source route for Qt/PySide/Shiboken components is LGPL version 3. The full LGPLv3 text and the incorporated GPLv3 base terms are supplied as `LGPL-3.0.txt` and `GPL-3.0.txt`.

The `source-manifest.json` preserves the observed wheel provenance and adds upstream Qt module/source-archive mappings for the Qt libraries and plugins in the preparation Analysis. QtSvg maps `Qt6Svg.dll`, `qsvg.dll` and `qsvgicon.dll`; Qt Image Formats maps `qicns`, `qtga`, `qtiff`, `qwbmp` and `qwebp`; Qt Base maps the other identified Qt libraries and plugins, including `qgif`, `qico` and `qjpeg`. The final D02 Analysis evidence remains authoritative for what is present in the frozen executable. GPL-only modules or unresolved license conflicts must not be included in a release.

Qt's official 6.8.3 third-party attribution JSON and relevant license/notice texts are included under `qt/6.8.3/`. They cover Qt Base's double-conversion, FreeType FTL/BDF/PCF/zlib, HarfBuzz, libjpeg, libpng, PCRE2/SLJIT and zlib materials, plus the Qt Image Formats libtiff and libwebp licenses and WebP patent notice. The JSON records are indexed and integrity-checked; the offline viewer displays the indexed `.txt` and `.md` license/notice text. Matching source archives and their sidecar digests are listed in `QT-SOURCE-AND-REBUILD.md` and `source-manifest.json`; the archives themselves have not been downloaded locally.

Copyright © The Qt Company Ltd. and contributors. Qt, PySide and Shiboken names and logos remain the property of their respective owners.

## Python runtime and pinned packages

The source manifest records the CPython 3.12.10 x64 runtime package, the runtime files observed in Analysis, and the corresponding CPython license materials. The official Python NuGet package digest is recorded alongside the tagged CPython source and package-provided license provenance. It also lists license files from each pinned Python distribution represented in Analysis, grouped by package and version. Those package texts include additional component and font notices where the distribution provides them. The inventory mapping is evidence about build inputs; it is not a legal conclusion about a particular binary.

## PyInstaller

PyInstaller 6.11.1 is the pinned freezer. Its full `COPYING.txt`, including the GPLv2-or-later terms and Bootloader Exception, is supplied as `PYINSTALLER-COPYING.txt`. PyInstaller identifies its runtime hooks and some runtime helper modules as Apache-2.0; the full Apache 2.0 text is supplied as `APACHE-2.0.txt`. D02's Analysis output determines which PyInstaller runtime pieces are present in the executable.

## Bundled CJK font

The bundled Noto Sans CJK SC font remains under the SIL Open Font License 1.1. Its complete license is `../fonts/OFL.txt`; source and version provenance are in `../fonts/font-source.json`.

Matplotlib's observed font data has separate materials: its upstream license, DejaVu and STIX notices, the BaKoMa font license, and the PDF core-font readme are included under `packages/matplotlib-3.9.4/` or `matplotlib/` and indexed for offline viewing.

## Microsoft runtime files and Windows dependencies

`MICROSOFT-RUNTIME-NOTICE.md` records the observed Microsoft C runtime DLL source groups and PE versions, summarizes the Microsoft redistribution conditions reviewed, and states the limits of what the inventory establishes. The corresponding official license and guidance text extracts are stored under `microsoft/`. The Windows 10/11 API-set and UCRT copies sourced from System32 were excluded from the post-filter Analysis; that OS behavior does not establish redistribution rights for arbitrary Windows files.

## Frozen component inventory

`source-manifest.json` records package/license groups and a 279-entry native preparation map from the post-System32-filter Analysis; it is not the final frozen-component inventory. The external D02 release evidence must identify every component in the final Analysis and map it to its source group and license materials. The PySide/Shiboken, Qt Base, QtSvg and Qt Image Formats archive digests were checked against Qt's official SHA-256 sidecars; the source archives have not been downloaded for local archive-hash verification. Neither a source digest nor a file name grants a redistribution right.

