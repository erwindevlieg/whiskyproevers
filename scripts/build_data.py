#!/usr/bin/env python3
"""Zet het tabblad Overzicht uit whisky_overzicht.xlsx om naar data.json.

Het Excel-bestand blijft de bron. Persoonlijke kolommen zoals
'Past bij smaak' en 'Toelichting smaak' worden bewust weggelaten.
Lege cellen worden null; de tekst 'onbekend' blijft staan zoals in de bron.
Er wordt niets verzonnen of aangevuld.
"""

from __future__ import annotations

import argparse
import json
import sys
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

    return {
        "generatedAt": date.today().isoformat(),
        "count": len(items),
        "items": items,
    }


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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print("Geschreven: %s (%s rijen)" % (output, payload["count"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
