"""Write build/version_info.txt, the Windows version resource for the exes,
and downpour/_buildinfo.py, which names the commit in every log.

    py tools/version_info.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downpour import __version__  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Downpour'),
      StringStruct('FileDescription', 'Downpour download manager'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('ProductName', 'Downpour'),
      StringStruct('ProductVersion', '{v}'),
      StringStruct('LegalCopyright', 'Downpour contributors')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    parts = [int(p) for p in __version__.split(".")[:3]]
    parts += [0] * (4 - len(parts))
    out = ROOT / "build" / "version_info.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(TEMPLATE.format(t=tuple(parts), v=__version__), encoding="utf-8")
    print(f"Wrote {out}")
    stamp = ROOT / "downpour" / "_buildinfo.py"
    stamp.write_text(f'"""Written by tools/version_info.py; not in git."""\n\nBUILD = {build_id()!r}\n', encoding="utf-8")
    print(f"Wrote {stamp}")


def build_id() -> str:
    """The commit and date of this build, which every log's first line repeats."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.strip()
        changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit, changed = "unknown", ""
    return f"build {commit}{'+changes' if changed else ''} {date.today().isoformat()}"


if __name__ == "__main__":
    main()
