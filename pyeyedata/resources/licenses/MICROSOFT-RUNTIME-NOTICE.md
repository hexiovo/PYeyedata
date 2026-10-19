# Microsoft runtime and Windows dependency research

This notice records the Microsoft runtime files observed in the post-System32-filter D02 Analysis inventory and the official terms reviewed for them. It is a source and provenance record, not a legal opinion or a grant of redistribution rights. Starting with 0.9.5 the release tooling packages the Windows executable for download and reconciles its actual native component paths and hashes with the maintained source/notice map. The source links and full license terms remain applicable to those components.

## Inventory observation

The filtered Analysis records 11 Microsoft C runtime DLL copies from the pinned CPython and Python package environment. PE version strings and the source-group mapping are summarized here; the source manifest records each archive path, digest, package group, and applicable notice material.

| Analysis source group | DLL names | PE file/product version |
| --- | --- | --- |
| CPython 3.12.0 installation | `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` | `14.34.31938.0` |
| PySide6_Essentials 6.8.3 | `MSVCP140.dll`, `MSVCP140_1.dll`, `MSVCP140_2.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` | `14.39.33520.0` |
| shiboken6 6.8.3 | `MSVCP140.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` | `14.39.33520.0` |
| pandas 2.2.3 | `pandas.libs/msvcp140-0f2ea95580b32bcfc81c235d5751ce78.dll` | `14.16.27033.0` |

These file paths identify copies found in the build inputs. Names, version strings, hashes, and package locations do not prove that a particular copy was obtained from a Microsoft redistributable package or that the applicable prerequisites for distributing that copy have been met.

The Windows System32 filter excluded the 41 reviewed API-set/UCRT copies sourced from the build host. Windows 10 and Windows 11 provide the OS API-set contracts and Universal CRT used by this build. The Windows SDK REDIST material below describes a separate SDK redistributable path; matching filenames in a System32 inventory do not establish SDK provenance or rights.

## Official materials and limits

The offline text extracts below were retrieved on 2026-10-16 and are listed with source URLs, original-document hashes, and extraction hashes in `source-manifest.json`:

- Microsoft Visual C++ 2015–2022 Runtime Software License Terms.
- Microsoft Visual C++ V14 Redistributable and Runtime Software License Terms.
- Microsoft guidance on redistributing Visual C++ files, the Visual Studio 2022 REDIST list, and the latest supported Visual C++ Redistributable.
- Microsoft Windows SDK license and REDIST list, Universal CRT deployment guidance, and Windows API-set documentation.

The Visual Studio and Windows SDK materials condition redistribution on applicable licensed products, listed files, unmodified form, and additional terms. They explain possible redistribution routes; they do not establish the licensing provenance of these observed DLL copies. The 2026 V14 and Visual Studio materials are references to their stated terms and do not mean a 2026 SDK or Visual Studio binary is included in this application.

The source manifest retains the exact observed file hashes and source-group mappings. The release builder checks the actual native Analysis inventory and indexed license materials before packaging; a changed native file requires an updated source/notice map. Release assets include that reconciled map as native-components.json. Applicable Microsoft license prerequisites remain governed by the included terms; this inventory does not claim a general or unrestricted binary redistribution right.
