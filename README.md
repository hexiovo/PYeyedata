# PYeyedata

PYeyedata is a Windows desktop application for eye-tracking data analysis. The supported operating systems are Windows 10 and Windows 11, x64.

## Build from source

Use 64-bit CPython 3.12.1 or a later 3.12.x release on a Windows 10/11 x64 build machine. CPython 3.12.10 is the validated runtime. Version 3.12.0 is below the build minimum because a code-object replacement bug can break PyInstaller's frozen SciPy startup. Install the pinned environment from `requirements.txt`, then run the checked-in one-file build runner with a new output directory outside the repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$releaseOutput = Join-Path $env:TEMP ("PYeyedata-release-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $releaseOutput | Out-Null
.\.venv\Scripts\python.exe -X utf8 -B .\build_windows.py --run $releaseOutput --phase final
```

The resulting `PYeyedata.exe`, build logs, and verification evidence are written under `$releaseOutput`. The recipe derives the project root from the checked-out source and does not require the original HexiPlan RUN or its audit files. PyInstaller Analysis omits only the reviewed API-set/UCRT names when their source is the build host's `System32`; same-named package files are retained, and any other System32-sourced binary fails the build. Windows 10/11 supplies the API-set contracts and Universal CRT used by the application.

The output records the actual build inputs and environment. Build artifacts are machine-specific; the procedure does not promise byte-identical EXEs across different Windows installations.

## Licenses

The application exposes its bundled third-party licenses and source notices through **Help → Licenses**. The source manifest identifies bundled runtime and resource components, their source references, and applicable notices. Review the Microsoft Visual C++ runtime conditions and component provenance before distributing a frozen binary outside the local application scope; the source tree and the build recipe do not establish a general binary redistribution right.
