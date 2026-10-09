"""Fetch NVDA's controller client DLL into downpour/vendor/.

Downpour speaks through the user's own NVDA (their voice, rate and verbosity)
using this LGPL library from NV Access. It is downloaded rather than committed
so the repository holds no third-party binaries.

    py tools/fetch_nvda_client.py [NVDA version]
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "2026.1"
VENDOR = Path(__file__).resolve().parent.parent / "downpour" / "vendor"


def main(argv: list[str]) -> int:
    version = argv[0] if argv else DEFAULT_VERSION
    url = f"https://download.nvaccess.org/releases/{version}/nvda_{version}_controllerClient.zip"
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        archive = zipfile.ZipFile(io.BytesIO(resp.read()))
    VENDOR.mkdir(parents=True, exist_ok=True)
    (VENDOR / "nvdaControllerClient.dll").write_bytes(archive.read("x64/nvdaControllerClient.dll"))
    (VENDOR / "nvdaControllerClient-license.txt").write_bytes(archive.read("license.txt"))
    print(f"Saved to {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
