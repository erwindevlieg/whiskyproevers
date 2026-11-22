#!/usr/bin/env python3
"""Zet het tabblad Overzicht uit whisky_overzicht.xlsx om naar data.json.

Het Excel-bestand blijft de bron. Persoonlijke kolommen zoals
'Past bij smaak' en 'Toelichting smaak' worden bewust weggelaten.
Lege cellen worden null; de tekst 'onbekend' blijft staan zoals in de bron.
Er wordt niets verzonnen of aangevuld.

Optioneel worden productomschrijvingen uit descriptions.json (van
scripts/fetch_descriptions.py) als 'omschrijving' en 'omschrijvingBron'
meegenomen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import openpyxl

# Kolommen die niet in de publieke data mogen.
SKIP_COLUMNS = {
    "Past bij smaak",
    "Toelichting smaak",
}

# Excel-kop → JSON-veld.
COLUMN_MAP = {
    "Naam": "naam",
    "Sample beschikbaar": "sampleBeschikbaar",
    "Sample-grootte": "inhoud",
    "Sampleprijs (€)": "prijsSample",
    "Whiskysite-link": "sampleLink",
    "ABV %": "abv",
    "Regio/stijl": "regio",
    "Vattype": "vattype",
    "Ongekoeld gefilterd": "ncf",
    "Kleurstof": "kleurstof",
    "Rookniveau": "rook",
    "Proefnotities kort": "proefnotities",
    "Bron proefnotities": "bronNotities",
    "WB-status": "wbStatus",
    "WB-ID": "wbId",
    "WB-link": "wbLink",
    "WB-score": "score",
    "WB-stemmen": "stemmen",
    "Reden WB-status": "wbReden",
    "Whiskyfun-score": "whiskyfunScore",
    "Whiskyfun-link": "whiskyfunLink",
    "Distiller-score + smaakprofiel": "distillerScore",
    "Distiller-link": "distillerLink",
    "Goedkoopste winkel": "winkel",
    "Flesprijs (€)": "flesPrijs",
    "Flesgrootte": "flesGrootte",
    "Fles-link": "flesLink",
    "Op voorraad": "opVoorraad",
    "Gecontroleerd op": "gecontroleerdOp",
    "Tweede goedkoopste winkel": "tweedeWinkel",
    "Tweede prijs (€)": "tweedePrijs",
    "Tweede link": "tweedeLink",
    "Opmerking fles": "opmerkingFles",
}

NUMBER_FIELDS = {
    "prijsSample",
    "abv",
    "score",
    "flesPrijs",
    "tweedePrijs",
}

INTEGER_FIELDS = {"stemmen"}

AGE_RE = re.compile(
    r"(?i)\b(\d{1,2})\s*(?:years?\s*old|jaar(?:\s*oud)?|yo)\b"
)


def slugify(text):
    """Stabiele, leesbare URL-slug: letters/cijfers en koppeltekens."""
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    return slug or "whisky"


def extract_age(name):
    """Leeftijd alleen als die letterlijk in de naam staat, anders weglaten."""
    if not name:
        return None
    match = AGE_RE.search(name)
    if not match:
        return None
    age = int(match.group(1))
    if age < 1 or age > 80:
        return None
    return age


def unique_key(item):
    return "|".join([
        item.get("sampleLink") or "",
        item.get("flesLink") or "",
        item.get("wbId") or "",
        item.get("naam") or "",
        item.get("inhoud") or "",
    ])


def assign_ids(items):
    used = set()
    for item in items:
        base = slugify(item["naam"])
        slug = base
        if slug in used:
            suffix = hashlib.sha1(unique_key(item).encode("utf-8")).hexdigest()[:6]
            slug = "%s-%s" % (base, suffix)
            n = 2
            while slug in used:
                slug = "%s-%s-%s" % (base, suffix, n)
                n += 1
        used.add(slug)
        # id vooraan zetten voor leesbaarheid in ruwe JSON-tools
        ordered = {"id": slug}
        age = extract_age(item.get("naam"))
        if age is not None:
            ordered["leeftijd"] = age
        ordered.update(item)
        item.clear()
        item.update(ordered)


def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_number(value, integer=False):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
        value = float(text)
    number = float(value)
    if integer:
        return int(round(number))
    if number.is_integer():
        return int(number)
    return number


def row_to_item(header_index, values):
    item = {}
    for excel_name, json_name in COLUMN_MAP.items():
        raw = values[header_index[excel_name]]
        if json_name in INTEGER_FIELDS:
            value = clean_number(raw, integer=True)
        elif json_name in NUMBER_FIELDS:
            value = clean_number(raw)
        else:
            value = clean_text(raw)
            if json_name == "gecontroleerdOp" and raw is not None and not isinstance(raw, str):
                # Datums als 22-11-2026 laten staan zoals in het bestand,
                # of formatteren als die als datum binnenkomen.
                try:
                    value = raw.strftime("%d-%m-%Y")
                except AttributeError:
                    value = clean_text(raw)
        if value is not None:
            item[json_name] = value
    return item


def build(xlsx_path: Path) -> dict:
    workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if "Overzicht" not in workbook.sheetnames:
        raise SystemExit("Tabblad 'Overzicht' ontbreekt in %s" % xlsx_path)
    sheet = workbook["Overzicht"]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    header = [str(h).strip() if h is not None else "" for h in header]

    missing = [name for name in COLUMN_MAP if name not in header]
    if missing:
        raise SystemExit("Ontbrekende kolommen in Overzicht: %s" % ", ".join(missing))

    unexpected_personal = [name for name in SKIP_COLUMNS if name not in header]
    # Geen fout als ze ontbreken; wel overslaan als ze er wel staan.
    header_index = {name: i for i, name in enumerate(header)}

    items = []
    for values in rows:
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        naam = clean_text(values[header_index["Naam"]])
        if not naam:
            continue
        items.append(row_to_item(header_index, values))
    workbook.close()

    for item in items:
        if "Past bij smaak" in item or "pastBijSmaak" in item:
            raise SystemExit("Persoonlijke kolom is per ongeluk meegenomen.")

    assign_ids(items)
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise SystemExit("Dubbele id's bij het genereren van slugs.")

    return {
        "generatedAt": date.today().isoformat(),
        "count": len(items),
        "items": items,
    }


def merge_descriptions(items, descriptions_path: Path) -> int:
    """Voeg letterlijke Whiskysite-omschrijvingen toe; ontbrekende blijven weg."""
    if not descriptions_path or not descriptions_path.is_file():
        return 0
    data = json.loads(descriptions_path.read_text(encoding="utf-8"))
    added = 0
    for item in items:
        entry = data.get(item.get("id"))
        if not entry:
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        item["omschrijving"] = text
        if entry.get("sourceUrl"):
            item["omschrijvingBron"] = entry["sourceUrl"]
        added += 1
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Genereer data.json uit het tabblad Overzicht van whisky_overzicht.xlsx."
    )
    parser.add_argument(
        "xlsx",
        type=Path,
        help="Pad naar whisky_overzicht.xlsx (niet in de repo; bevat persoonlijke kolommen).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Uitvoerbestand (standaard: data.json naast deze repo-root).",
    )
    parser.add_argument(
        "--descriptions",
        type=Path,
        default=None,
        help="Pad naar descriptions.json (standaard: naast het uitvoerbestand, indien aanwezig).",
    )
    args = parser.parse_args(argv)

    xlsx_path = args.xlsx.expanduser().resolve()
    if not xlsx_path.is_file():
        raise SystemExit("Bestand niet gevonden: %s" % xlsx_path)

    output = args.output
    if output is None:
        output = Path(__file__).resolve().parent.parent / "data.json"
    else:
        output = output.expanduser().resolve()

    payload = build(xlsx_path)
    desc_path = args.descriptions
    if desc_path is None:
        desc_path = output.parent / "descriptions.json"
    else:
        desc_path = desc_path.expanduser().resolve()
    added = merge_descriptions(payload["items"], desc_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        "Geschreven: %s (%s rijen, %s met omschrijving)" % (output, payload["count"], added),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
