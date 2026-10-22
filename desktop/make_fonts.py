# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Zen's type for the launcher's own windows (desktop/Zen.cs): Google Sans Flex as Zen's page sets it (web/app.css:
rounded, its dark-page grade), made into two fixed fonts Windows can load - Zen Sans, regular and semibold - in
desktop/fonts. build.py puts them inside the exe. Run again only when the font changes:

    .venv/Scripts/python.exe desktop/make_fonts.py

The page's file is WOFF2, which needs Brotli to read: the brotli module, or else Node's own (zlib), nothing installed.
Google Sans Flex is under the SIL Open Font License 1.1 with no reserved name; these are modified versions of it, named
apart (Zen Sans) as the license asks of a modified font, with its license beside them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "web" / "fonts" / "google-sans-flex-latin.woff2"
OUT = REPO / "desktop" / "fonts"
DIALS = {"ROND": 100, "GRAD": 100}  # web/app.css's --dials on :root
WEIGHTS = {"Regular": 400, "SemiBold": 600}


def node_brotli() -> None:
    """A stand-in brotli module that asks Node to decompress (fontTools only calls decompress)."""
    node = shutil.which("node")
    if node is None:
        sys.exit("WOFF2 needs Brotli: pip install brotli, or put Node on PATH")
    script = "process.stdout.write(require('zlib').brotliDecompressSync(require('fs').readFileSync(0)))"
    module = types.ModuleType("brotli")
    module.decompress = lambda data: subprocess.run([node, "-e", script], input=data, capture_output=True, check=True).stdout
    sys.modules["brotli"] = module


def main() -> None:
    try:
        import brotli  # noqa: F401
    except ImportError:
        node_brotli()
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    OUT.mkdir(exist_ok=True)
    for style, weight in WEIGHTS.items():
        font = TTFont(SOURCE)
        axes = {a.axisTag: a.defaultValue for a in font["fvar"].axes} | {"wght": weight}
        axes.update({tag: value for tag, value in DIALS.items() if tag in axes})
        font = instantiateVariableFont(font, axes)
        # a fixed font needs no style table - and Windows would name the family from it ("Zen Sans Rounded")
        del font["STAT"]
        # one family per weight, as Windows' older font model has it: "Zen Sans" and "Zen Sans SemiBold"
        family = "Zen Sans" if style == "Regular" else f"Zen Sans {style}"
        names = font["name"]
        for record in list(names.names):
            if record.nameID in (1, 2, 3, 4, 6, 16, 17, 21, 22, 25) or record.nameID >= 256:
                names.removeNames(nameID=record.nameID)
        for name_id, text in ((1, family), (2, "Regular"), (3, f"Zen Sans {style}; from Google Sans Flex"),
                              (4, f"Zen Sans {style}"), (6, f"ZenSans-{style}"), (16, "Zen Sans"), (17, style)):
            names.setName(text, name_id, 3, 1, 0x409)
        font["OS/2"].usWeightClass = weight
        font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0b1100001) | 0b1000000  # REGULAR only
        font["head"].macStyle = 0
        font.flavor = None
        font.save(OUT / f"ZenSans-{style}.ttf")
        print(f"{style}: {axes} -> {(OUT / f'ZenSans-{style}.ttf').stat().st_size // 1024} KB")
    shutil.copy2(REPO / "web" / "fonts" / "OFL-google-sans-flex.txt", OUT / "OFL.txt")


if __name__ == "__main__":
    main()
