#!/usr/bin/env python3
"""Controle: lijst en detailpagina tonen exact dezelfde tags.

Faalt (exit 1) als
- een rij in de index geen `tg` heeft terwijl de tags uit index + detailgegevens er wel zijn,
- `tg` verschilt van de tags die uit de volledige gegevens (index + detail-chunk) volgen,
- een tag in `tg` niet is in te delen (regio, vat, kenmerk of smaak) en dus op de pagina zou ontbreken,
- index.html tags zelf opnieuw berekent in plaats van `tg` te gebruiken.

Gebruik: python scripts/check_tags.py [repo]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_site as ex  # noqa: E402


def js_list(html: str, name: str) -> list[str]:
    m = re.search(r"const %s = \[(.*?)\];" % name, html, re.S)
    if not m:
        raise SystemExit("%s niet gevonden in index.html" % name)
    return re.findall(r'"([^"]+)"', m.group(1))


def main(argv=None) -> int:
    repo = Path((argv or sys.argv[1:] or [Path(__file__).resolve().parent.parent])[0]).resolve()
    manifest = json.loads((repo / "data.json").read_text(encoding="utf-8"))
    index = json.loads((repo / manifest["index"]).read_text(encoding="utf-8"))
    chunks: dict = {}
    for path in manifest["chunks"]:
        chunks.update(json.loads((repo / path).read_text(encoding="utf-8")))
    html = (repo / "index.html").read_text(encoding="utf-8")

    groups = set(js_list(html, "REGIO_ORDER")) | set(js_list(html, "VAT_ORDER")) | set(js_list(html, "KENMERK_LABELS"))
    groups |= {lab for _, lab, _ in ex.FLAVOUR}
    errors: list[str] = []

    for banned in ("function flavourTags", "function deriveVat", "function parseRegio", "flavourTags(", "deriveVat(", "parseRegio("):
        if banned in html:
            errors.append("index.html berekent tags zelf opnieuw: %s" % banned)

    checked = differ = unsorted_tags = 0
    for compact in index["items"]:
        checked += 1
        item = {
            "regio": compact.get("r"),
            "vattype": compact.get("v"),
            "rook": compact.get("k"),
            "proefnotities": compact.get("o"),
            "ncf": compact.get("c"),
            "kleurstof": compact.get("z"),
        }
        item.update(chunks.get(compact["id"]) or {})
        expected = ex.all_tags(item)
        got = compact.get("tg") or []
        if got != expected:
            differ += 1
            if len(errors) < 20:
                errors.append("%s: index %s != detail %s" % (compact["id"], got, expected))
        for tag in got:
            if tag not in groups:
                unsorted_tags += 1
                if len(errors) < 20:
                    errors.append("%s: tag %r niet in te delen" % (compact["id"], tag))

    print("Gecontroleerd: %s rijen, %s verschillen, %s niet-indeelbare tags" % (checked, differ, unsorted_tags))
    for line in errors:
        print("  FOUT:", line)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
