#!/usr/bin/env python3
"""Zet het tabblad Overzicht uit whisky_overzicht.xlsx om naar data.json.

Het Excel-bestand blijft de bron. Persoonlijke kolommen zoals
'Past bij smaak' en 'Toelichting smaak' worden bewust weggelaten.
Lege cellen worden null; de tekst 'onbekend' blijft staan zoals in de bron.
Er wordt niets verzonnen of aangevuld.

Optioneel worden productomschrijvingen uit descriptions.json (van
scripts/fetch_descriptions.py) als 'omschrijving', 'omschrijvingBron' en 'afbeelding'
meegenomen.

Samples van dezelfde whisky in 3 cl en 6 cl worden tot één rij samengevoegd.
Alleen zekere matches (zelfde naam na het weghalen van de samplemaat, plus
dezelfde ABV en leeftijd). Oude id's blijven bereikbaar via 'redirects'.
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
        # id vooraan zetten voor leesbaarheid in ruwe JSON-tools.
        # De berekende slug wint altijd, ook als het item al een oude id had.
        ordered = {"id": slug}
        age = extract_age(item.get("naam"))
        if age is not None:
            ordered["leeftijd"] = age
        ordered.update(item)
        ordered["id"] = slug
        if age is not None:
            ordered["leeftijd"] = age
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


SIZE_TRAIL_RE = re.compile(
    r"(?:[\s\-–—]*(?:\d+(?:[.,]\d+)?\s*cl(?:\s*sample)?|sample|monster|proefmonster))+\s*$",
    re.I,
)
SIZE_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*cl", re.I)
UNKNOWN_RE = re.compile(r"^onbekend\b", re.I)
VOLUME_PREAMBLE_RE = re.compile(
    r"^0[.,]0\d\s*ltrs?\.?\s*(?:\d+[.,]?\d*\s*%\s*)?(?:\n+)?",
    re.I,
)
EMPTY_CONTAINER_RE = re.compile(
    r"lege/.empty sample fles|lege sample fles|empty sample bottle",
    re.I,
)
WB_STATUS_RANK = {
    "exact": 4,
    "labelversie": 3,
    "andere versie": 2,
    "nog niet opgezocht": 1,
    "niet van toepassing": 0,
}


def strip_sample_markers(naam):
    """Haal alleen een trailing samplemaat (3 cl, 6 cl, sample) van de naam."""
    text = (naam or "").strip()
    cleaned = SIZE_TRAIL_RE.sub("", text).strip(" -–—")
    return re.sub(r"\s+", " ", cleaned)


def normalize_name_key(naam):
    text = strip_sample_markers(naam).lower()
    text = text.replace("&", "and")
    text = re.sub(r"[“”\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_sample_size(item):
    """Normalizeer '3 cl' / '3,0 cl' naar een canonieke grootte-string, of None."""
    text = " ".join(part for part in (item.get("inhoud"), item.get("naam")) if part)
    match = SIZE_VALUE_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    value = float(raw)
    if value.is_integer():
        number = str(int(value))
    else:
        number = raw.rstrip("0").rstrip(".") if "." in raw else raw
    return "%s cl" % number


def size_sort_key(inhoud):
    match = SIZE_VALUE_RE.search(inhoud or "")
    if not match:
        return (1, inhoud or "")
    return (0, float(match.group(1).replace(",", ".")))


def is_missing(value):
    if value is None or value == "":
        return True
    if isinstance(value, str) and UNKNOWN_RE.match(value.strip()):
        return True
    return False


def is_empty_container(item):
    reden = (item.get("wbReden") or "").lower()
    if "lege fles" in reden or "geen whisky-sample" in reden:
        return True
    return bool(EMPTY_CONTAINER_RE.search(item.get("naam") or ""))


def group_key(item):
    return (normalize_name_key(item.get("naam")), item.get("abv"), item.get("leeftijd"))


def richness(item):
    score = 0
    for key, value in item.items():
        if key in {"id", "aliases", "samples"}:
            continue
        if not is_missing(value):
            score += 1
    if item.get("afbeelding"):
        score += 3
    if item.get("omschrijving"):
        score += 2
    if item.get("score") is not None:
        score += 2
    if item.get("flesPrijs") is not None:
        score += 2
    if item.get("stemmen"):
        score += item["stemmen"] / 1000.0
    status = (item.get("wbStatus") or "").strip().lower()
    score += WB_STATUS_RANK.get(status, 0) * 0.1
    return score


def pick_value(left, right, field):
    a, b = left.get(field), right.get(field)
    if is_missing(a) and not is_missing(b):
        return b
    if is_missing(b) and not is_missing(a):
        return a
    if field == "omschrijving" and isinstance(a, str) and isinstance(b, str):
        return a if len(a) >= len(b) else b
    if field == "stemmen" and a is not None and b is not None:
        return a if a >= b else b
    if field == "score":
        sa, sb = left.get("stemmen") or 0, right.get("stemmen") or 0
        if sa != sb:
            return a if sa > sb else b
    if field == "wbStatus":
        ra = WB_STATUS_RANK.get(str(a or "").strip().lower(), 0)
        rb = WB_STATUS_RANK.get(str(b or "").strip().lower(), 0)
        return a if ra >= rb else b
    return a if not is_missing(a) else b


def sample_entry(item):
    entry = {}
    inhoud = parse_sample_size(item) or item.get("inhoud")
    if inhoud:
        entry["inhoud"] = inhoud
    if item.get("prijsSample") is not None:
        entry["prijsSample"] = item["prijsSample"]
    if item.get("sampleLink"):
        entry["sampleLink"] = item["sampleLink"]
    if item.get("id"):
        entry["id"] = item["id"]
    if item.get("naam"):
        entry["naam"] = item["naam"]
    return entry


def attach_samples_array(item):
    if item.get("samples"):
        return
    if item.get("sampleBeschikbaar") == "nee" and item.get("prijsSample") is None and not item.get("sampleLink"):
        return
    entry = {}
    if item.get("inhoud"):
        entry["inhoud"] = item["inhoud"]
    if item.get("prijsSample") is not None:
        entry["prijsSample"] = item["prijsSample"]
    if item.get("sampleLink"):
        entry["sampleLink"] = item["sampleLink"]
    if entry:
        item["samples"] = [entry]


def merge_group(rows):
    """Voeg zekere 3 cl/6 cl-rijen samen; behoud de rijkste overige velden."""
    ordered = sorted(rows, key=lambda item: size_sort_key(parse_sample_size(item) or item.get("inhoud") or ""))
    base = max(ordered, key=richness)
    merged = dict(base)
    for other in ordered:
        if other is base:
            continue
        for field in other.keys():
            if field in {"id", "naam", "inhoud", "prijsSample", "sampleLink", "samples", "aliases"}:
                continue
            merged[field] = pick_value(merged, other, field)
            # pick_value uses merged as left, so also fill from other when left is missing
            if is_missing(merged.get(field)) and not is_missing(other.get(field)):
                merged[field] = other[field]
        # paired score/stemmen: if we took the other score, take its stemmen/wb too
    # If scores came from different rows, keep the score of the row with more votes.
    best_scored = max(
        (row for row in ordered if row.get("score") is not None),
        key=lambda row: (row.get("stemmen") or 0, richness(row)),
        default=None,
    )
    if best_scored is not None:
        for field in ("score", "stemmen", "wbStatus", "wbId", "wbLink", "wbReden"):
            if field in best_scored:
                merged[field] = best_scored[field]

    samples = [sample_entry(row) for row in ordered if sample_entry(row)]
    merged["samples"] = samples
    cleaned = strip_sample_markers(base.get("naam") or "")
    if cleaned:
        merged["naam"] = cleaned
    sizes = [s.get("inhoud") for s in samples if s.get("inhoud")]
    if sizes:
        merged["inhoud"] = " / ".join(sizes)
    priced = [s for s in samples if s.get("prijsSample") is not None]
    if priced:
        cheapest = min(priced, key=lambda s: s["prijsSample"])
        merged["prijsSample"] = cheapest["prijsSample"]
        if cheapest.get("sampleLink"):
            merged["sampleLink"] = cheapest["sampleLink"]
    elif "prijsSample" in merged:
        del merged["prijsSample"]
    if any(row.get("sampleBeschikbaar") == "ja" for row in ordered):
        merged["sampleBeschikbaar"] = "ja"
    if merged.get("omschrijving") and len(rows) > 1:
        stripped = VOLUME_PREAMBLE_RE.sub("", merged["omschrijving"], count=1).strip()
        if stripped:
            merged["omschrijving"] = stripped
    merged["aliases"] = [row["id"] for row in ordered if row.get("id")]
    return merged


def classify_group(rows):
    """Alleen zekere 3 cl + 6 cl (of andere verschillende samplematen) samenvoegen."""
    if any(is_empty_container(row) for row in rows):
        return "ambiguous", "lege flesjes of andere verpakking, geen samples van dezelfde whisky"
    sizes = [parse_sample_size(row) for row in rows]
    if any(size is None for size in sizes):
        return "ambiguous", "niet van alle rijen is een samplemaat af te leiden"
    unique = set(sizes)
    if len(unique) == 1:
        return "ambiguous", "zelfde samplemaat, mogelijk dubbele winkel-URL"
    # only merge typical sample sizes, never a 70 cl fles with a sample
    def cl_value(label):
        match = SIZE_VALUE_RE.search(label or "")
        return float(match.group(1).replace(",", ".")) if match else None
    values = [cl_value(size) for size in sizes]
    if any(v is None or v > 20 for v in values):
        return "ambiguous", "minstens één maat lijkt geen sample"
    if len(rows) == 2 and unique <= {"3 cl", "6 cl"}:
        return "merge", "3 cl en 6 cl van dezelfde whisky"
    if len(unique) == len(rows):
        return "merge", "verschillende samplematen van dezelfde whisky"
    return "ambiguous", "meer rijen dan unieke maten"


def merge_sample_sizes(payload):
    """Voeg zekere sample-paren samen en lever een redirects-mapping plus rapport."""
    items = payload["items"]
    groups = {}
    for item in items:
        groups.setdefault(group_key(item), []).append(item)

    merged_for = {}
    ambiguous = []
    merged_pairs = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        decision, reason = classify_group(rows)
        label = strip_sample_markers(rows[0].get("naam") or key[0])
        sizes = [parse_sample_size(row) or row.get("inhoud") or "?" for row in rows]
        info = {
            "naam": label,
            "reden": reason,
            "rijen": [
                {
                    "id": row.get("id"),
                    "naam": row.get("naam"),
                    "inhoud": parse_sample_size(row) or row.get("inhoud"),
                    "prijsSample": row.get("prijsSample"),
                    "sampleLink": row.get("sampleLink"),
                }
                for row in rows
            ],
        }
        if decision == "merge":
            merged_for[id(rows[0])] = (rows, merge_group(rows))
            for extra in rows[1:]:
                merged_for[id(extra)] = None
            merged_pairs.append(info)
        else:
            ambiguous.append(info)

    new_items = []
    for item in items:
        marker = merged_for.get(id(item), "keep")
        if marker == "keep":
            attach_samples_array(item)
            new_items.append(item)
        elif marker is None:
            continue
        else:
            _rows, merged = marker
            new_items.append(merged)

    assign_ids(new_items)
    redirects = {}
    for item in new_items:
        for old_id in item.get("aliases") or []:
            if old_id and old_id != item["id"]:
                redirects[old_id] = item["id"]

    payload["items"] = new_items
    payload["count"] = len(new_items)
    payload["redirects"] = redirects
    return {
        "merged": merged_pairs,
        "ambiguous": ambiguous,
        "before": len(items),
        "after": len(new_items),
    }


def merge_descriptions(items, descriptions_path: Path) -> tuple[int, int]:
    """Voeg letterlijke Whiskysite-omschrijvingen en afbeeldings-URL's toe."""
    if not descriptions_path or not descriptions_path.is_file():
        return 0, 0
    data = json.loads(descriptions_path.read_text(encoding="utf-8"))
    added = 0
    images = 0
    for item in items:
        entry = data.get(item.get("id"))
        if not entry:
            for alias in item.get("aliases") or []:
                entry = data.get(alias)
                if entry:
                    break
        if not entry:
            continue
        text = (entry.get("text") or "").strip()
        if text:
            item["omschrijving"] = text
            if entry.get("sourceUrl"):
                item["omschrijvingBron"] = entry["sourceUrl"]
            added += 1
        image = (entry.get("image") or "").strip()
        if image.startswith("https://"):
            item["afbeelding"] = image
            images += 1
    return added, images


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Genereer de site-data uit het tabblad Overzicht van whisky_overzicht.xlsx."
    )
    parser.add_argument(
        "xlsx",
        type=Path,
        nargs="?",
        default=None,
        help="Pad naar whisky_overzicht.xlsx (niet in de repo; bevat persoonlijke kolommen).",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="Bestaande data.json opnieuw publiceren (compacte index, details, thumbs) zonder Excel.",
    )
    parser.add_argument(
        "--skip-thumbs",
        action="store_true",
        help="Bestaande thumbs in assets/thumbs niet opnieuw bouwen.",
    )
    parser.add_argument(
        "--thumb-cache",
        type=Path,
        default=None,
        help="Cache-map voor originele productfoto's (standaard /workspace/whisky_build/imgcache).",
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

    output = args.output
    if output is None:
        output = Path(__file__).resolve().parent.parent / "data.json"
    else:
        output = output.expanduser().resolve()

    added = images = 0
    report = {"merged": [], "ambiguous": [], "before": 0, "after": 0}

    if args.from_json:
        src = args.from_json.expanduser().resolve()
        if not src.is_file():
            raise SystemExit("Bestand niet gevonden: %s" % src)
        payload = json.loads(src.read_text(encoding="utf-8"))
        if "items" not in payload:
            raise SystemExit("Geen items in %s" % src)
        report["before"] = report["after"] = payload.get("count", len(payload["items"]))
        added = sum(1 for it in payload["items"] if it.get("omschrijving"))
        images = sum(1 for it in payload["items"] if it.get("afbeelding"))
    else:
        if args.xlsx is None:
            raise SystemExit("Geef een Excel-bestand of --from-json.")
        xlsx_path = args.xlsx.expanduser().resolve()
        if not xlsx_path.is_file():
            raise SystemExit("Bestand niet gevonden: %s" % xlsx_path)
        payload = build(xlsx_path)
        desc_path = args.descriptions
        if desc_path is None:
            desc_path = output.parent / "descriptions.json"
        else:
            desc_path = desc_path.expanduser().resolve()
        added, images = merge_descriptions(payload["items"], desc_path)
        report = merge_sample_sizes(payload)

    output.parent.mkdir(parents=True, exist_ok=True)

    # Volledige payload blijft de bron voor de publicatie; data.json wordt
    # daarna de kleine manifest. Een kopie staat naast de uitvoer als
    # .full-data.json en wordt niet gecommit.
    full_copy = output.parent / ".full-data.json"
    full_copy.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    from export_site import export_site
    from thumbs import DEFAULT_CACHE

    cache = args.thumb_cache or DEFAULT_CACHE
    export_site(
        payload,
        output.parent,
        cache_dir=cache,
        skip_thumbs=args.skip_thumbs,
    )

    print(
        "Bron: %s rijen, %s met omschrijving, %s met afbeelding (kopie: %s)"
        % (payload["count"], added, images, full_copy),
        file=sys.stderr,
    )
    if not args.from_json:
        print(
            "Samples samengevoegd: %s paren (%s → %s rijen)"
            % (len(report["merged"]), report["before"], report["after"]),
            file=sys.stderr,
        )
    if report["merged"]:
        for info in report["merged"]:
            sizes = ", ".join(
                "%s%s" % (row.get("inhoud") or "?", (" " + str(row["prijsSample"])) if row.get("prijsSample") is not None else "")
                for row in info["rijen"]
            )
            print("  - %s (%s)" % (info["naam"], sizes), file=sys.stderr)
    if report["ambiguous"]:
        print("Niet samengevoegd (%s groepen):" % len(report["ambiguous"]), file=sys.stderr)
        for info in report["ambiguous"]:
            print("  - %s: %s" % (info["naam"], info["reden"]), file=sys.stderr)
            for row in info["rijen"]:
                print(
                    "      %s | %s | %s | %s"
                    % (row.get("id"), row.get("naam"), row.get("inhoud"), row.get("sampleLink") or ""),
                    file=sys.stderr,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
