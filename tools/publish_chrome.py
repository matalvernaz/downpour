"""Send the extension to the Chrome Web Store, or ask how it is doing there.

    python tools/publish_chrome.py status    what the store says about the item
    python tools/publish_chrome.py upload    upload dist\\store\\downpour-chrome-<version>.zip
    python tools/publish_chrome.py publish   upload it, then submit it for review
    python tools/publish_chrome.py cancel    withdraw the submission under review

It signs in as the Google Cloud service account named on the developer
dashboard's Settings page. That account's JSON key never goes in the
repository: it is read from %USERPROFILE%\\.secrets\\downpour-cws-service-account.json,
or from the path in DOWNPOUR_CWS_KEY. Signing the sign-in request needs the
cryptography package (pip install cryptography).

Build the zip first with tools/make_store_assets.py. The store only accepts
a version higher than the last one it was given, so raise "version" in
extension/manifest.json before each upload.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from downpour.browser.ids import CHROME_WEB_STORE_ID  # noqa: E402

PUBLISHER_ID = os.environ.get("DOWNPOUR_CWS_PUBLISHER", "7fc76c34-476b-4fc8-bd6d-ed2c8021efb4")
KEY_PATH = Path(os.environ.get("DOWNPOUR_CWS_KEY", Path.home() / ".secrets" / "downpour-cws-service-account.json"))
SCOPE = "https://www.googleapis.com/auth/chromewebstore"
API = f"https://chromewebstore.googleapis.com/v2/publishers/{PUBLISHER_ID}/items/{CHROME_WEB_STORE_ID}"
UPLOAD = f"https://chromewebstore.googleapis.com/upload/v2/publishers/{PUBLISHER_ID}/items/{CHROME_WEB_STORE_ID}:upload"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def access_token() -> str:
    """Trade a signed JWT for a one-hour access token (Google's service
    account flow). The private key is used here and nowhere else."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        sys.exit("This needs the cryptography package: pip install cryptography")
    try:
        info = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    except OSError as e:
        sys.exit(f"Could not read the service account key at {KEY_PATH}: {e.strerror or e}")
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": info["private_key_id"]}
    claims = {"iss": info["client_email"], "scope": SCOPE, "aud": info["token_uri"], "iat": now, "exp": now + 3600}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}".encode("ascii")
    key = serialization.load_pem_private_key(info["private_key"].encode("ascii"), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": f"{signing_input.decode('ascii')}.{_b64(signature)}",
    }).encode("ascii")
    reply = _call("POST", info["token_uri"], body, {"Content-Type": "application/x-www-form-urlencoded"})
    return reply["access_token"]


def _call(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"{method} {url.split('?')[0]} failed: HTTP {e.code} {e.reason}\n{detail}")
    return json.loads(text) if text.strip() else {}


def summary(reply: dict) -> str:
    """fetchStatus in a few lines a screen reader gets through quickly."""
    lines = []
    for label, key in (("Published", "publishedItemRevisionStatus"), ("Submitted", "submittedItemRevisionStatus")):
        revision = reply.get(key)
        if not revision:
            lines.append(f"{label}: nothing")
            continue
        versions = ", ".join(c.get("crxVersion", "?") for c in revision.get("distributionChannels", []))
        lines.append(f"{label}: {revision.get('state', 'unknown state')}" + (f", version {versions}" if versions else ""))
    if reply.get("lastAsyncUploadState"):
        lines.append(f"Last upload: {reply['lastAsyncUploadState']}")
    if reply.get("takenDown"):
        lines.append("TAKEN DOWN for a policy violation; the dashboard has details")
    if reply.get("warned"):
        lines.append("Policy warning: fix it or the item is taken down; the dashboard has details")
    return "\n".join(lines)


def _zip() -> Path:
    version = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
    path = ROOT / "dist" / "store" / f"downpour-chrome-{version}.zip"
    if not path.is_file():
        sys.exit(f"{path} is missing; run tools/make_store_assets.py first")
    return path


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    if action not in ("status", "upload", "publish", "cancel"):
        print(__doc__.strip())
        return 2
    auth = {"Authorization": f"Bearer {access_token()}"}
    if action == "status":
        print(summary(_call("GET", f"{API}:fetchStatus", headers=auth)))
        return 0
    if action == "cancel":
        reply = _call("POST", f"{API}:cancelSubmission", b"", auth)
    else:
        path = _zip()
        print(f"Uploading {path.name}")
        reply = _call("POST", UPLOAD, path.read_bytes(), {**auth, "Content-Type": "application/zip"})
        print(json.dumps(reply, indent=2))
        if action == "upload":
            return 0
        print("Submitting it for review")
        reply = _call("POST", f"{API}:publish", b"", auth)
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
