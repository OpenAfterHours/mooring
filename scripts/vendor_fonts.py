"""Vendor the OFL display + mono fonts into the hub's static assets.

Run ONCE on a machine with internet access. The hub UI ships **Space Grotesk**
for its headings and **JetBrains Mono** for its structural type (the chart-room
rails, paths, state cells and receipt ledger), but the app must NEVER fetch
fonts at runtime — the frozen ``.pyz`` / ``.exe`` runs on air-gapped machines,
so the bytes have to travel *inside* the build. This script pulls each family's
latin-subset, variable-weight woff2 (one file covers the whole weight axis) plus
the SIL OFL licence into ``src/mooring/hub/static/fonts/`` so the next
``uv build`` / moonlit build carries them.

    uv run python scripts/vendor_fonts.py

Both families are licensed under the SIL Open Font License 1.1, which permits
redistribution; each family's licence is vendored alongside its font for
attribution (``OFL.txt`` for Space Grotesk, ``OFL-JetBrainsMono.txt`` for
JetBrains Mono). Re-run this to refresh a font (e.g. a new upstream version);
commit the resulting woff2.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Google Fonts' css2 endpoint serves the smallest, latin-subset woff2 when asked
# with a modern browser UA; without one it falls back to a fat legacy TTF.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

_DEST = Path(__file__).resolve().parent.parent / "src" / "mooring" / "hub" / "static" / "fonts"


@dataclass(frozen=True)
class Font:
    """One vendored family: where to fetch it and what to call it on disk."""

    css_url: str
    woff2_name: str
    licence_url: str
    licence_name: str
    label: str


_FONTS = (
    Font(
        css_url="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400..700&display=swap",
        woff2_name="space-grotesk.woff2",
        licence_url="https://raw.githubusercontent.com/floriankarsten/space-grotesk/master/OFL.txt",
        licence_name="OFL.txt",
        label="Space Grotesk",
    ),
    Font(
        css_url="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400..700&display=swap",
        woff2_name="jetbrains-mono.woff2",
        licence_url="https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/OFL.txt",
        licence_name="OFL-JetBrainsMono.txt",
        label="JetBrains Mono",
    ),
)


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

    for font in _FONTS:
        css = _get(font.css_url).decode("utf-8")
        woff2 = _get(_latin_woff2_url(css))
        if woff2[:4] != b"wOF2":
            raise SystemExit(f"Downloaded {font.label} is not a valid woff2 (bad magic).")
        (_DEST / font.woff2_name).write_bytes(woff2)
        (_DEST / font.licence_name).write_bytes(_get(font.licence_url))
        print(f"Vendored {font.label} ({len(woff2)} bytes) + {font.licence_name} -> {_DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
