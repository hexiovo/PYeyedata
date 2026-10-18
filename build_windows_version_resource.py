"""Generate PyInstaller's Windows version-resource source from APP_VERSION."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
APP_INFO_PATH = PROJECT_ROOT / "pyeyedata" / "app_info.py"
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _read_release_identity() -> tuple[str, str]:
    tree = ast.parse(APP_INFO_PATH.read_text(encoding="utf-8"), filename=str(APP_INFO_PATH))
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id in {"APP_NAME", "APP_VERSION"}:
                value = ast.literal_eval(statement.value)
                if not isinstance(value, str):
                    raise ValueError(f"{target.id} must be a string constant")
                values[target.id] = value

    if set(values) != {"APP_NAME", "APP_VERSION"}:
        raise ValueError("app_info.py must define APP_NAME and APP_VERSION exactly once")
    if not values["APP_NAME"].strip():
        raise ValueError("APP_NAME cannot be empty")
    if not _VERSION_PATTERN.fullmatch(values["APP_VERSION"]):
        raise ValueError("APP_VERSION must use three numeric components: major.minor.patch")
    return values["APP_NAME"], values["APP_VERSION"]


def render_version_resource() -> str:
    """Return a PyInstaller version-file expression with no copied version literal."""
    app_name, app_version = _read_release_identity()
    parts = tuple(int(part) for part in app_version.split(".")) + (0,)
    version_text = ".".join(str(part) for part in parts)
    version_tuple = "(" + ", ".join(str(part) for part in parts) + ")"
    app_name_literal = repr(app_name)
    description_literal = repr(f"{app_name} eye-tracking data analysis")
    filename_literal = repr(f"{app_name}.exe")
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [StringTable(
        '040904B0',
        [
          StringStruct('FileDescription', {description_literal}),
          StringStruct('FileVersion', {version_text!r}),
          StringStruct('InternalName', {app_name_literal}),
          StringStruct('OriginalFilename', {filename_literal}),
          StringStruct('ProductName', {app_name_literal}),
          StringStruct('ProductVersion', {version_text!r})
        ]
      )]
    ),
    VarFileInfo([VarStruct('Translation', [0x409, 1200])])
  ]
)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="write the generated PyInstaller version file here")
    args = parser.parse_args(argv)
    try:
        text = render_version_resource()
        compile(text, "<generated-pyinstaller-version-resource>", "eval")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
        _, version = _read_release_identity()
        print(f"APP_VERSION={version}")
        print(f"version_resource={args.output.resolve()}")
        return 0
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"Cannot generate PyInstaller version resource: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
