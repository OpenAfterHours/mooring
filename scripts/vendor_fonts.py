"""Vendor the OFL Space Grotesk display font into the hub's static assets.

Run ONCE on a machine with internet access. The hub UI ships Space Grotesk for
its headings, but the app must NEVER fetch fonts at runtime — the frozen
``.pyz`` / ``.exe`` runs on air-gapped machines, so the bytes have to travel
*inside* the build. This script pulls the latin-subset, variable-weight woff2
(one file covers the whole 300-700 axis) plus the SIL OFL licence into
``src/mooring/hub/static/fonts/`` so the next ``uv build`` / moonlit build
carries them.

    uv run python scripts/vendor_fonts.py

Space Grotesk is licensed under the SIL Open Font License 1.1, which permits
redistribution; OFL.txt (with the original copyright) is vendored alongside the
font for attribution. Re-run this to refresh the font (e.g. a new upstream
version); commit the resulting woff2.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

# Google Fonts' css2 endpoint serves the smallest, latin-subset woff2 when asked
# with a modern browser UA; without one it falls back to a fat legacy TTF.
_CSS_URL = "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400..700&display=swap"
_OFL_URL = "https://raw.githubusercontent.com/floriankarsten/space-grotesk/master/OFL.txt"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

_DEST = Path(__file__).resolve().parent.parent / "src" / "mooring" / "hub" / "static" / "fonts"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted hosts)
        return resp.read()


def _latin_woff2_url(css: str) -> str:
    """Return the src URL of the @font-face block whose range covers basic latin."""
    blocks = css.split("@font-face")
    for block in blocks:
        if "U+0000-00FF" in block:  # the latin subset
            m = re.search(r"url\((https://[^)]+\.woff2)\)", block)
            if m:
                return m.group(1)
    raise SystemExit("Could not find a latin-subset woff2 in the Google Fonts CSS.")


def main() -> int:
    _DEST.mkdir(parents=True, exist_ok=True)

    css = _get(_CSS_URL).decode("utf-8")
    woff2 = _get(_latin_woff2_url(css))
    if woff2[:4] != b"wOF2":
        raise SystemExit("Downloaded font is not a valid woff2 (bad magic).")
    (_DEST / "space-grotesk.woff2").write_bytes(woff2)

    (_DEST / "OFL.txt").write_bytes(_get(_OFL_URL))

    print(f"Vendored Space Grotesk ({len(woff2)} bytes) + OFL.txt -> {_DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
