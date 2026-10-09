"""Names that tie the extension, the native host and the registry together.

They are spelled out in places that cannot import each other - the
extension's manifest.json and background.js, the host manifests written by
``install`` and the browsers' registry keys - so they are collected here, and
a test checks that the extension still agrees with them.
"""

from __future__ import annotations

import base64
import hashlib

# The native-messaging host name: lowercase letters, digits, dots and
# underscores only, and it doubles as the registry key name.
HOST_NAME = "io.github.matalvernaz.downpour"

# Chromium derives an extension's ID from the public key in the "key" field of
# its manifest.json, so an unpacked copy has the same ID on every machine and
# the host manifest can name it in advance. IDs assigned by the Chrome Web
# Store or Edge Add-ons belong in this list too once the extension is published
# there.
CHROMIUM_EXTENSION_IDS: list[str] = ["biniilicelamnmgkghjfbhajjmamlldn"]

# Firefox takes the ID verbatim from browser_specific_settings.gecko.id.
FIREFOX_EXTENSION_ID = "downpour@matalvernaz.github.io"


def chromium_id_from_key(key: str) -> str:
    """The extension ID Chromium gives a manifest "key" (base64 DER public key).

    It is the first 32 hex digits of the key's SHA-256, each digit spelled
    with the letters a to p.
    """
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(digit, 16)) for digit in digest)
