"""Write build/version_info.txt, the Windows version resource for the exes.

    py tools/version_info.py
"""

from __future__ import annotations

import sys
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


if __name__ == "__main__":
    main()
