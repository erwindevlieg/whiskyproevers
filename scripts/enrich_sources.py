#!/usr/bin/env python3
"""Vul ontbrekende omschrijvingen en proefnotities aan uit echte bronnen.

Verzin nooit data. Alleen zekere productmatches (zelfde whisky, leeftijd,
editie en vat) worden overgenomen, letterlijk, met bron-URL. Master of Malt
alleen voor omschrijving en proefnotities, nooit voor prijzen of aankooplinks.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import sources as src

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path("/workspace/whisky_build")
SHOP_CACHE = BUILD / "shopcache"
WNL_CACHE = BUILD / "wnlcache"
DD_SITEMAP = BUILD / "ddcache" / "sitemap-urls.txt"
WNL_SITEMAP = BUILD / "enrich" / "whisky-nl-sitemap.xml"
ENRICH_DIR = BUILD / "enrich"
FETCHED_DIR = ENRICH_DIR / "fetched"
USER_AGENT = (
    "Mozilla/5.0 (compatible; whisky-overzicht/1.0; "
    "+https://github.com/erwindevlieg/whiskyproevers)"
)

AGE_RE = re.compile(
    r"(?i)\b(\d{1,2})\s*(?:years?\s*old|year\s*old|years?\b|jaar(?:\s*oud)?|yo|y\b)"
)
ABV_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")
BATCH_RE = re.compile(
    r"(?i)\bbatch\s*(?:#|no\.?|nr\.?|number)?\s*([a-z]?\d+(?:/\d+)?|viii|vii|iii|ii|iv|vi|ix|xi|xii|v|x|i)\b"
)
ROMAN_BATCH = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12",
}
CASK_RE = re.compile(
    r"(?i)\bcask\s*(?:#|no\.?|nr\.?|number|num(?:mer)?)?\s*(\d+[a-z]?)(?!\s*%)\b"
)
VINTAGE_RE = re.compile(r"\b((?:19[5-9]\d|20[0-2]\d))\b")
VOLUME_RE = re.compile(
    r"(?ix)\b\d+(?:[.,]\d+)?\s*(?:cl|ml|ltr|l)\b"
    r"|\b(?:3|6)\s*cl\s*sample\b"
    r"|\bsample\b"
)
RELEASE_YEAR_RE = re.compile(
    r"(?i)\b(?:bottled(?:\s+in)?|release|edition|limited)\s+(\d{4})\b"
    r"|\b(\d{4})\s+(?:release|edition)\b"
)

STOPWORDS = {
    "the", "and", "of", "a", "an", "de", "het", "een", "old", "year", "years",
    "jaar", "oud", "yo", "single", "malt", "whisky", "whiskey", "scotch",
    "irish", "sample", "cl", "ml", "ltr", "liter", "cask", "strength", "cs",
    "edition", "release", "limited", "bottled", "for", "with", "by", "from",
}

# Tokens die een editie uniek maken; een conflict is een mismatch.
EDITION_CANON = {
    "px": "pedro ximenez",
    "pedro": "pedro ximenez",
    "ximenez": "pedro ximenez",
    "ximenez": "pedro ximenez",
    "parliament": "parliament",
    "allardice": "allardice",
    "solist": "solist",
    "abunadh": "abunadh",
    "bunadh": "abunadh",
    "casg": "casg annamh",
    "annamh": "casg annamh",
    "sherry": "sherry",
    "oloroso": "oloroso",
    "fino": "fino",
    "manzanilla": "manzanilla",
    "port": "port",
    "portwood": "port",
    "pineau": "pineau",
    "armagnac": "armagnac",
    "madeira": "madeira",
    "marsala": "marsala",
    "bourbon": "bourbon",
    "rum": "rum",
    "wine": "wine",
    "harmony": "harmony",
    "rare": "rare cask",
    "classic": "classic cut",
    "luminary": "luminary",
    "suspended": "suspended time",
    "cigar": "cigar malt",
    "speymalt": "speymalt",
    "chinquapin": "chinquapin",
    "parliament": "parliament",
    "sample": "sample room",
    "makers": "makers cut",
    "maker's": "makers cut",
    "night": "night on earth",
    "jerez": "night on earth",
    "vibrant": "vibrant oak",
    "green": "green meadow",
    "jing": "jing",
    "triple": "triple cask",
    "double": "double cask",
    "peated": "peated",
    "unpeated": "unpeated",
}

WNL_CUT_RE = re.compile(
    r"(?is)("
    r"bestel deze whisky|"
    r"bestel deze|"
    r"whisky\.nl is de beste webshop|"
    r"single malt whisky is een pure malt die afkomstig|"
    r"whisky is een heerlijke whisky van het merk|"
    r"is een heerlijke whisky van het merk"
    r").*"
)
CTA_RE = re.compile(
    r"(?i)("
    r"bestel nu|voordelig online kopen|bekijk prijs en beschikbaarheid|"
    r"in de webshop|voor advies kom langs of bel|"
    r"bij slijterij|vandaag verzonden|besteld op werkdagen|"
    r"nu besteld\s*=\s*morgen in huis|gegarandeerd de beste prijs|"
    r"gratis verzending|incl\.?\s*btw|direct leverbaar|"
    r"op = op|in winkelwagen"
    r")"
)
GENERIC_MEASURE_RE = re.compile(
    r"""
    ^(?:
        \d+(?:[.,]\d+)?\s*(?:ltr|l\b|cl|ml)\.?
        |\d+(?:[.,]\d+)?\s*%
        |[,;/.\-\s]
    )+$
    """,
    re.IGNORECASE | re.VERBOSE,
)

SOURCE_LABELS = {
    "whiskysite.nl": "Whiskysite.nl",
    "drankdozijn.nl": "Drankdozijn",
    "whisky.nl": "Whisky.nl",
    "masterofmalt.com": "Master of Malt",
    "bockje.nl": "'t Bockje",
    "dramshop.nl": "Dramshop.nl",
    "whiskyestate.nl": "Whisky Estate",
    "woudenbergdranken.nl": "Woudenberg Dranken",
    "drankgigant.nl": "Drankgigant.nl",
    "mitra-oirschot.nl": "Mitra Oirschot",
    "gevoslijterij.nl": "Gevo Slijterij",
    "drankenshopbams.nl": "Drankenshop Bams",
    "whiskyvanzuylen.nl": "Whisky van Zuylen",
    "silersshop.nl": "Silersshop.nl",
    "dewhiskyspecialist.nl": "De Whisky Specialist",
    "zeewijck.nl": "Zeewijck",
    "whiskyxl.nl": "WhiskyXL",
    "melgers.nl": "Melgers",
    "dramtime.nl": "Dramtime.nl",
    "gall.nl": "Gall & Gall",
}


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    return slug or "whisky"


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    stripped = stripped.lower().replace("&", " and ").replace("'", "")
    stripped = re.sub(r"[^a-z0-9]+", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def parse_abv(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = ABV_RE.search(str(value).replace(",", "."))
    if match:
        return float(match.group(1).replace(",", "."))
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def features(name: str, abv=None) -> dict:
    raw = name or ""
    folded = fold(raw)
    age = None
    m = AGE_RE.search(raw)
    if m:
        age = int(m.group(1))
    found_abv = parse_abv(abv)
    if found_abv is None:
        m = ABV_RE.search(raw.replace(",", "."))
        if m:
            found_abv = float(m.group(1).replace(",", "."))
    batches = set()
    for m in BATCH_RE.finditer(raw):
        val = m.group(1).lower()
        batches.add(ROMAN_BATCH.get(val, val))
    casks = {m.group(1).lower() for m in CASK_RE.finditer(raw)}
    release_years = set()
    for m in RELEASE_YEAR_RE.finditer(raw):
        year = m.group(1) or m.group(2)
        if year:
            release_years.add(year)
    vintages = set()
    for m in VINTAGE_RE.finditer(raw):
        year = m.group(1)
        if year not in release_years:
            # leeftijd + jaartal in dezelfde naam: jaartal is distilleerjaar
            vintages.add(year)
    tokens = [t for t in folded.split() if t and t not in STOPWORDS and not t.isdigit()]
    editions = set()
    for tok in tokens:
        if tok in EDITION_CANON:
            editions.add(EDITION_CANON[tok])
    # extra meerwoord-edities
    if "cigar" in folded and "malt" in folded:
        editions.add("cigar malt")
    if "classic" in folded and "cut" in folded:
        editions.add("classic cut")
    if "rare" in folded and "cask" in folded:
        editions.add("rare cask")
    if "cask" in folded and "strength" in folded:
        editions.add("cask strength")
    if "100" in folded and "proof" in folded:
        editions.add("100 proof")
    if "110" in folded and "proof" in folded:
        editions.add("110 proof")
    if "pedro" in folded or re.search(r"\bpx\b", folded):
        editions.add("pedro ximenez")
    if "sherry" in folded and "oak" in folded:
        editions.add("sherry oak")
    if "double" in folded and "cask" in folded:
        editions.add("double cask")
    if "triple" in folded and "cask" in folded:
        editions.add("triple cask")
    return {
        "folded": folded,
        "tokens": tokens,
        "token_set": set(tokens),
        "age": age,
        "abv": found_abv,
        "batches": batches,
        "casks": casks,
        "vintages": vintages,
        "release_years": release_years,
        "editions": editions,
    }


TWO_WORD_BRAND = {
    "glen", "royal", "old", "isle", "ben", "loch", "port", "four", "wild",
    "jack", "catoctin", "makers", "maker", "woodford", "buffalo", "craig",
    "white", "red", "green", "yellow", "saint", "st", "kil", "van", "de",
    "el", "la", "the", "little", "brown", "compass", "johnnie", "chivas",
}


def brand_head(tokens: list[str]) -> list[str]:
    toks = list(tokens)
    if toks and toks[0] == "the":
        toks = toks[1:]
    if not toks:
        return []
    if toks[0] in TWO_WORD_BRAND and len(toks) > 1:
        return [toks[0], toks[1]]
    return [toks[0]]


def same_brand(a_name: str, b_name: str, a_feat=None, b_feat=None) -> bool:
    a = a_feat or features(a_name)
    b = b_feat or features(b_name)
    ha, hb = brand_head(a["tokens"]), brand_head(b["tokens"])
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    # "glen grant" vs "glengrant"
    if "".join(ha) == "".join(hb) or "".join(ha) == hb[0] or "".join(hb) == ha[0]:
        return True
    return False


def confident_match(item_name: str, candidate_name: str, item_abv=None, cand_abv=None) -> bool:
    """True alleen bij een zekere match: zelfde whisky, leeftijd, editie en vat."""
    a = features(item_name, item_abv)
    b = features(candidate_name, cand_abv)
    if not a["tokens"] or not b["tokens"]:
        return False
    if not same_brand(item_name, candidate_name, a, b):
        return False
    if a["age"] is not None and b["age"] is not None and a["age"] != b["age"]:
        return False
    if a["age"] is not None and b["age"] is None:
        # kandidaat zonder leeftijd is alleen oké als de naam verder zeer specifiek is
        # en de leeftijd als los getal in de kandidaat staat
        if str(a["age"]) not in b["folded"].split():
            return False
    if b["age"] is not None and a["age"] is None:
        if str(b["age"]) not in a["folded"].split():
            return False
    if a["batches"] or b["batches"]:
        if not a["batches"] or not b["batches"] or a["batches"].isdisjoint(b["batches"]):
            return False
    if a["casks"]:
        if not b["casks"] or a["casks"].isdisjoint(b["casks"]):
            return False
    if a["vintages"] and b["vintages"] and a["vintages"].isdisjoint(b["vintages"]):
        return False
    if a["vintages"] and not b["vintages"]:
        # ons item noemt een distilleerjaar; kandidaat moet dat jaar bevatten
        if not any(y in b["folded"].split() for y in a["vintages"]):
            return False
    if a["release_years"] and b["release_years"] and a["release_years"].isdisjoint(b["release_years"]):
        return False
    if a["release_years"] and not b["release_years"]:
        if not any(y in b["folded"].split() for y in a["release_years"]):
            return False
    # Edities: als wij een kenmerkende editie hebben, moet die terugkomen.
    distinctive = a["editions"] - {"sherry", "bourbon"}
    if distinctive and not distinctive.issubset(b["editions"] | set(b["folded"].split())):
        # soepeler: elk distinctief label moet als tekst voorkomen
        missing = []
        for ed in distinctive:
            if ed in b["editions"] or ed in b["folded"]:
                continue
            missing.append(ed)
        if missing:
            return False
    # Conflicterende edities (PX vs Fino, Parliament vs Allardice, etc.)
    conflict_groups = [
        {"pedro ximenez", "fino", "manzanilla", "oloroso"},
        {"parliament", "allardice"},
        {"classic cut", "rare cask", "harmony", "night on earth", "vibrant oak", "green meadow", "jing"},
        {"cigar malt"},
        {"abunadh", "casg annamh", "triple cask", "double cask", "sherry oak"},
        {"110 proof", "100 proof"},
    ]
    for group in conflict_groups:
        in_a = a["editions"] & group
        in_b = b["editions"] & group
        if in_a and in_b and in_a != in_b:
            return False
    # ABV: bij single cask / solist / uniek vatnummer exact; anders ±0.15
    av, bv = a["abv"], b["abv"]
    if av is not None and bv is not None:
        strict = bool(a["casks"] or b["casks"] or "solist" in a["editions"] or "solist" in b["editions"])
        if strict and abs(av - bv) > 0.11:
            return False
        if not strict and abs(av - bv) > 0.3:
            # core-range: soms noemt de ene bron 43 en de andere niets in de titel
            # maar als beide een ABV hebben en die sterk verschilt, afwijzen
            return False
    # Te weinig overlap in tokens? afwijzen als de overlap heel klein is
    overlap = len(a["token_set"] & b["token_set"])
    if overlap < 2 and not (a["age"] and a["age"] == b["age"] and same_brand("", "", a, b)):
        return False
    return True


def host_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def source_label(url: str) -> str:
    host = host_of(url)
    if host in SOURCE_LABELS:
        return SOURCE_LABELS[host]
    return host


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # veelvoorkomende mojibake
    for broken, fixed in {
        "â€™": "’", "â€˜": "‘", "â€œ": "“", "â€": "”",
        "â€“": "–", "â€”": "—", "â€¦": "…",
        "Ã©": "é", "Ã¨": "è", "Ãª": "ê", "Ãx": "ë",
    }.items():
        text = text.replace(broken, fixed)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


READMORE_LINE_RE = re.compile(
    r"(?i)^(?:lees meer|lees minder|lees verder|toon meer|toon minder)\s*\.?\s*$"
)


def strip_readmore_lines(text: str) -> str:
    """Verwijder alleen hele regels die precies een 'lees meer'-CTA zijn.

    Zinnen als 'Lees meer over deze whisky in...' blijven staan.
    """
    if not text:
        return ""
    kept = [line for line in text.split("\n") if not READMORE_LINE_RE.match(line.strip())]
    cleaned = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def strip_boilerplate(text: str) -> str:
    text = clean_text(text)
    text = strip_readmore_lines(text)
    text = WNL_CUT_RE.sub("", text).strip()
    # verwijder losse CTA-zinnen
    kept = []
    for para in re.split(r"\n\s*\n", text):
        if CTA_RE.search(para) and len(CTA_RE.sub("", para).strip()) < 80:
            continue
        kept.append(para)
    return "\n\n".join(kept).strip()


def is_useful_description(text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 90:
        return False
    if GENERIC_MEASURE_RE.match(compact):
        return False
    if CTA_RE.search(compact) and len(CTA_RE.sub("", compact).strip()) < 90:
        return False
    # te generiek winkelzinnetje
    if re.search(r"(?i)bij slijterij.*\d+\s*%.*\d+\s*cl", compact) and len(compact) < 220:
        return False
    return True


def jsonld_products(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        more = []
        for item in stack:
            if isinstance(item, dict) and "@graph" in item:
                graph = item["@graph"]
                more.extend(graph if isinstance(graph, list) else [graph])
            else:
                more.append(item)
        for item in more:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(x).lower() == "product" for x in types):
                out.append(item)
    return out


def text_from_jsonld(product: dict) -> tuple[str, str]:
    name = product.get("name") or ""
    desc = product.get("description") or ""
    if isinstance(desc, dict):
        desc = desc.get("text") or desc.get("@value") or ""
    if isinstance(name, list):
        name = name[0] if name else ""
    return str(name), str(desc)


def extract_shop_description(html_text: str, url: str) -> tuple[str, str]:
    """Geeft (naam, omschrijving) uit een winkelpagina, of lege strings."""
    if not html_text or len(html_text) < 500:
        return "", ""
    soup = BeautifulSoup(html_text, "lxml")
    host = host_of(url)
    name = ""
    desc = ""
    for product in jsonld_products(soup):
        n, d = text_from_jsonld(product)
        if n and not name:
            name = n
        if d and len(d) > len(desc):
            desc = d
    if soup.title and not name:
        name = soup.title.get_text(" ", strip=True)
    # host-specifieke fallbacks als json-ld leeg of nutteloos is
    if not is_useful_description(strip_boilerplate(desc)):
        candidates = []
        selectors = [
            "#description .product.attribute.description",
            ".product.attribute.description",
            ".product-description",
            "#description",
            "[itemprop=description]",
            ".woocommerce-product-details__short-description",
        ]
        if host == "drankgigant.nl":
            selectors = ["#description", ".product-description"] + selectors
        if host == "whiskyestate.nl":
            selectors = [".product__description", ".product-single__description", "[itemprop=description]"] + selectors
        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            for tag in el.select("script, style"):
                tag.decompose()
            txt = el.get_text("\n", strip=True)
            if txt:
                candidates.append(txt)
        for txt in candidates:
            if len(txt) > len(desc):
                desc = txt
    name = clean_text(html.unescape(name))
    desc = strip_boilerplate(desc)
    return name, desc


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / (digest + ".html")


def load_items(repo: Path) -> list[dict]:
    manifest = json.loads((repo / "data.json").read_text(encoding="utf-8"))
    index = json.loads((repo / manifest["index"]).read_text(encoding="utf-8"))
    chunks: dict = {}
    for path in manifest["chunks"]:
        chunks.update(json.loads((repo / path).read_text(encoding="utf-8")))
    items = []
    for compact in index["items"]:
        row = {
            "id": compact["id"],
            "naam": compact.get("n"),
            "leeftijd": compact.get("l"),
            "abv": compact.get("a"),
            "regio": compact.get("r"),
            "vattype": compact.get("v"),
            "score": compact.get("s"),
            "prijsSample": compact.get("p"),
            "flesPrijs": compact.get("f"),
            "inhoud": compact.get("h"),
            "proefnotities": compact.get("o"),
        }
        extra = chunks.get(compact["id"]) or {}
        row.update(extra)
        items.append(row)
    return items


def item_urls(item: dict) -> list[str]:
    urls = []
    for key in ("sampleLink", "flesLink", "tweedeLink"):
        if item.get(key):
            urls.append(item[key])
    for sample in item.get("samples") or []:
        if sample.get("sampleLink"):
            urls.append(sample["sampleLink"])
    seen = set()
    out = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def load_descriptions(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def maybe_add_description(descriptions: dict, item_id: str, text: str, url: str, stats: Counter) -> bool:
    text = src.format_description(strip_boilerplate(text))
    if not is_useful_description(text):
        stats["rejected_quality"] += 1
        return False
    prev = descriptions.get(item_id) or {}
    if prev.get("text") and is_useful_description(prev["text"]):
        stats["already_had"] += 1
        return False
    entry = dict(prev)
    entry["text"] = text
    entry["sourceUrl"] = url
    descriptions[item_id] = entry
    stats[source_label(url)] += 1
    stats["added"] += 1
    return True


def _note_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(text)).strip()


def note_pieces(item: dict, entry: dict | None) -> list[str]:
    """Alle bekende notitieteksten van een whisky: Excel/bron plus notes.json (hoofdtekst en extra's)."""
    pieces = []
    base = (item.get("proefnotities") or "").strip()
    if base and base.lower() != "onbekend":
        pieces.append(base)
    entry = entry or {}
    if entry.get("text"):
        pieces.append(entry["text"])
    for extra in entry.get("extra") or []:
        if extra.get("text"):
            pieces.append(extra["text"])
    return pieces


def maybe_add_notes(notes: dict, item: dict, text: str, url: str, stats: Counter) -> bool:
    """Voeg een proefnotitie toe uit een extra bron (samenvoegen, niet overschrijven).

    De eerste notitie van een whisky staat in notes.json als text/sourceUrl/source;
    elke volgende, andere notitie komt in 'extra'. Een tekst die al (deels) bekend is,
    wordt overgeslagen.
    """
    text = clean_text(text)
    if not text or len(re.sub(r"\s+", " ", text)) < 40:
        return False
    entry = notes.get(item["id"]) or {}
    key = _note_key(text)
    for piece in note_pieces(item, entry):
        other = _note_key(piece)
        if key == other or key in other or (other and other in key and len(other) > 40):
            stats["notes_already"] += 1
            return False
    new = {"text": text, "sourceUrl": url, "source": source_label(url)}
    if not entry.get("text"):
        entry = dict(entry)
        entry.update(new)
        notes[item["id"]] = entry
    else:
        entry = dict(entry)
        entry["extra"] = list(entry.get("extra") or []) + [new]
        notes[item["id"]] = entry
    stats["notes_added"] += 1
    stats["notes:" + source_label(url)] += 1
    return True


def apply_shopcache(items: list[dict], descriptions: dict, stats: Counter) -> None:
    by_url = {}
    for item in items:
        if item.get("omschrijving") or (descriptions.get(item["id"]) or {}).get("text"):
            continue
        for url in item_urls(item):
            host = host_of(url)
            if host in ("whiskysite.nl", "whiskybase.com", "shop.whiskybase.com"):
                continue
            by_url.setdefault(url, []).append(item)
    for url, group in by_url.items():
        path = cache_path(SHOP_CACHE, url)
        if not path.is_file():
            stats["shop_uncached"] += 1
            continue
        html_text = path.read_text(encoding="utf-8", errors="replace")
        name, desc = extract_shop_description(html_text, url)
        if not is_useful_description(desc):
            stats["shop_no_text"] += 1
            continue
        for item in group:
            label = name or Path(urlparse(url).path).name
            if not confident_match(item["naam"], label, item.get("abv")):
                # De URL staat al als fleslink in de data: dat is een door de
                # bronverzameling bevestigde koppeling. We eisen nog wel dat
                # de paginatitel niet over een ánder product gaat.
                # Als de titel te ver afwijkt, slaan we over.
                stats["shop_title_mismatch"] += 1
                continue
            maybe_add_description(descriptions, item["id"], desc, url, stats)


def slug_name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    leaf = path.split("/")[-1]
    leaf = re.sub(r"\.html?$", "", leaf, flags=re.I)
    leaf = re.sub(r"^(fles|doos|box|artikel)-", "", leaf, flags=re.I)
    leaf = leaf.replace("-", " ")
    leaf = VOLUME_RE.sub(" ", leaf)
    return re.sub(r"\s+", " ", leaf).strip()


def is_gift_or_box(url: str) -> bool:
    leaf = urlparse(url).path.lower()
    return bool(re.search(r"(^|/)(doos|box|giftset|gift-?box|tasting-collection|miniaturen)", leaf))


def match_sitemaps(items: list[dict], descriptions: dict | None = None) -> dict:
    """Koppel ontbrekende items aan zekere sitemap-URL's (één kandidaat)."""
    descriptions = descriptions or {}
    missing = [
        it for it in items
        if not it.get("omschrijving")
        and not (descriptions.get(it["id"]) or {}).get("text")
    ]
    wnl_urls = []
    if WNL_SITEMAP.is_file():
        text = WNL_SITEMAP.read_text(encoding="utf-8", errors="replace")
        wnl_urls = re.findall(r"<loc>([^<]+)</loc>", text)
        wnl_urls = [
            u for u in wnl_urls
            if u.endswith(".html")
            and "/whisky-merken/" not in u
            and "/whisky-soorten/" not in u
            and "/whisky-landen/" not in u
            and "/whisky-leeftijden/" not in u
            and not is_gift_or_box(u)
        ]
    dd_urls = []
    if DD_SITEMAP.is_file():
        dd_urls = [
            line.strip()
            for line in DD_SITEMAP.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("http")
            and "/artikel/fles-" in line
            and not is_gift_or_box(line)
        ]

    def prepare_index(urls: list[str]) -> dict:
        from collections import defaultdict
        idx = defaultdict(list)
        for url in urls:
            cand = slug_name_from_url(url)
            feat = features(cand)
            if not feat["tokens"]:
                continue
            rec = (url, cand, feat)
            for tok in feat["tokens"][:3]:
                idx[tok].append(rec)
        return idx

    def unique_matches(idx: dict, item: dict) -> list[str]:
        feat = features(item["naam"], item.get("abv"))
        hits = []
        seen = set()
        for tok in feat["tokens"][:3]:
            for url, cand, cand_feat in idx.get(tok, []):
                if url in seen:
                    continue
                seen.add(url)
                if feat["age"] is not None and cand_feat["age"] is not None and feat["age"] != cand_feat["age"]:
                    continue
                if confident_match(item["naam"], cand, item.get("abv"), cand_feat["abv"]):
                    hits.append(url)
        if len(hits) <= 1:
            return hits
        names = {fold(slug_name_from_url(u)) for u in hits}
        if len(names) == 1:
            return [hits[0]]
        return []

    wnl_idx = prepare_index(wnl_urls)
    dd_idx = prepare_index(dd_urls)

    result = {"whisky.nl": {}, "drankdozijn.nl": {}}
    for item in missing:
        wnl = unique_matches(wnl_idx, item)
        if wnl:
            result["whisky.nl"][item["id"]] = {"url": wnl[0], "naam": item["naam"]}
        dd = unique_matches(dd_idx, item)
        if dd:
            # als het item al een drankdozijn-link heeft, gebruik die
            existing = [u for u in item_urls(item) if host_of(u) == "drankdozijn.nl"]
            result["drankdozijn.nl"][item["id"]] = {
                "url": existing[0] if existing else dd[0],
                "naam": item["naam"],
            }
        elif any(host_of(u) == "drankdozijn.nl" for u in item_urls(item)):
            existing = [u for u in item_urls(item) if host_of(u) == "drankdozijn.nl"]
            result["drankdozijn.nl"][item["id"]] = {"url": existing[0], "naam": item["naam"]}
    return result


def fetch_html(session: requests.Session, url: str, cache_dir: Path, delay: float) -> tuple[str | None, str]:
    path = cache_path(cache_dir, url)
    if path.is_file() and path.stat().st_size > 500:
        return path.read_text(encoding="utf-8", errors="replace"), "cache"
    try:
        time.sleep(delay)
        response = session.get(url, timeout=30)
        if response.status_code >= 400:
            return None, "HTTP %s" % response.status_code
        encoding = response.apparent_encoding or response.encoding or "utf-8"
        try:
            text = response.content.decode(encoding, errors="replace")
        except LookupError:
            text = response.content.decode("utf-8", errors="replace")
        path.write_text(text, encoding="utf-8")
        path.with_suffix(".url").write_text(url + "\n", encoding="utf-8")
        return text, "fetch"
    except requests.RequestException as exc:
        return None, str(exc)


def fetch_whisky_nl(matches: dict, descriptions: dict, stats: Counter, delay: float) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"})
    WNL_CACHE.mkdir(parents=True, exist_ok=True)
    items_by_id = matches  # id -> {url, naam}
    for index, (item_id, rec) in enumerate(items_by_id.items(), start=1):
        url = rec["url"]
        html_text, status = fetch_html(session, url, WNL_CACHE, delay)
        if not html_text:
            stats["wnl_fetch_fail"] += 1
            print("Mislukt whisky.nl: %s (%s)" % (url, status), file=sys.stderr)
            continue
        if status == "cache":
            stats["wnl_cache"] += 1
        else:
            stats["wnl_fetch"] += 1
        name, desc = extract_shop_description(html_text, url)
        label = name or slug_name_from_url(url)
        if not confident_match(rec["naam"], label, None):
            stats["wnl_mismatch"] += 1
            continue
        maybe_add_description(descriptions, item_id, desc, url, stats)
        if index == 1 or index % 25 == 0 or index == len(items_by_id):
            print(
                "whisky.nl %s/%s  nieuw=%s  cache=%s  toegevoegd bron Whisky.nl=%s"
                % (index, len(items_by_id), stats["wnl_fetch"], stats["wnl_cache"], stats["Whisky.nl"]),
                file=sys.stderr,
                flush=True,
            )


MOM_NOSE_RE = re.compile(r"(?im)^###\s*Nose\s*$")
MOM_PALATE_RE = re.compile(r"(?im)^###\s*Palate\s*$")
MOM_FINISH_RE = re.compile(r"(?im)^###\s*Finish\s*$")


def parse_mom_markdown(md: str) -> dict:
    """Haal titel, omschrijving en neus/smaak/afdronk uit WebFetch-markdown."""
    if not md:
        return {}
    # verwijder prijs- en bestelblokken grofweg
    lines = md.replace("\r\n", "\n").split("\n")
    title = ""
    for line in lines:
        if line.startswith("# ") and "Master Of Malt" not in line and "Master of Malt" not in line:
            title = line[2:].strip()
            title = re.sub(r"\s*70cl\s*Whisky\s*$", "", title, flags=re.I).strip()
            break
    if not title:
        m = re.search(r"^#\s+(.+)$", md, re.M)
        if m:
            title = m.group(1).strip()
    # ABV en leeftijd uit de productregel
    abv = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", md)
    if m:
        abv = float(m.group(1).replace(",", "."))
    def section_after(header_re: re.Pattern, stop_res: list[re.Pattern]) -> str:
        m = header_re.search(md)
        if not m:
            return ""
        start = m.end()
        end = len(md)
        for stop in stop_res:
            n = stop.search(md, start)
            if n and n.start() < end:
                end = n.start()
        # ook stoppen bij volgende ## kop die niet Nose/Palate/Finish is
        n = re.search(r"\n##\s+", md[start:end])
        if n:
            end = start + n.start()
        return clean_text(md[start:end])

    desc = ""
    m = re.search(r"(?im)^##\s*Description\s*$", md)
    if m:
        stop = [MOM_NOSE_RE, re.compile(r"(?im)^##\s+Style\s*$"), re.compile(r"(?im)^###\s+")]
        desc = section_after(re.compile(r"(?im)^##\s*Description\s*$"), stop)
        # MoM toont de beschrijving soms dubbel
        paras = [p.strip() for p in re.split(r"\n\s*\n", desc) if p.strip()]
        unique = []
        seen = set()
        for p in paras:
            key = re.sub(r"\s+", " ", p.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        desc = "\n\n".join(unique).strip()
    nose = section_after(MOM_NOSE_RE, [MOM_PALATE_RE, MOM_FINISH_RE])
    palate = section_after(MOM_PALATE_RE, [MOM_FINISH_RE, re.compile(r"(?im)^##\s+")])
    finish = section_after(MOM_FINISH_RE, [re.compile(r"(?im)^##\s+"), re.compile(r"(?im)^Style\s*$")])
    # gooi CTA / prijzen uit notities
    def clean_note(text: str) -> str:
        text = clean_text(text)
        text = re.sub(r"(?is)(We charge a flat rate|Price drop|Save £|In stock, get it|Free delivery).*", "", text).strip()
        return text
    nose, palate, finish = clean_note(nose), clean_note(palate), clean_note(finish)
    notes_parts = []
    if nose:
        notes_parts.append("Neus: " + re.sub(r"\s+", " ", nose).strip())
    if palate:
        notes_parts.append("Smaak: " + re.sub(r"\s+", " ", palate).strip())
    if finish:
        notes_parts.append("Afdronk: " + re.sub(r"\s+", " ", finish).strip())
    return {
        "name": title,
        "abv": abv,
        "description": strip_boilerplate(desc),
        "notes": "\n".join(notes_parts).strip(),
    }


def parse_dd_markdown(md: str) -> dict:
    """Haal titel en productomschrijving uit Drankdozijn-WebFetch-markdown."""
    if not md:
        return {}
    title = ""
    m = re.search(r"^#\s+(.+)$", md, re.M)
    if m:
        title = m.group(1).strip()
    abv = None
    m = re.search(r"(?i)Alcoholpercentage\s+(\d+(?:[.,]\d+)?)\s*%", md)
    if m:
        abv = float(m.group(1).replace(",", "."))
    # De beschrijving staat typisch na de eigenschappen-tabelkop (Land ... Smaakprofiel)
    # en vóór '### Eigenschappen'. Pak alinea's tussen de eerste productkop en Eigenschappen,
    # zonder prijs/voorraad-regels.
    body = md
    start = 0
    if title:
        idx = md.find("# " + title)
        if idx >= 0:
            start = idx + len("# " + title)
    end = len(md)
    m = re.search(r"(?im)^###\s*Eigenschappen\s*$", md)
    if m:
        end = m.start()
    chunk = md[start:end]
    paras = []
    for raw in re.split(r"\n\s*\n", chunk):
        para = clean_text(raw)
        if not para:
            continue
        compact = re.sub(r"\s+", " ", para)
        if re.search(r"(?i)incl\.?\s*btw|direct leverbaar|gratis verzending|zondag voor|prijsklasse|€\d|\d+\.\d{2}\s+\d+", compact):
            continue
        if re.match(r"(?i)^(land|smaakprofiel|70cl|vol & rond)\b", compact):
            continue
        if len(compact) < 60:
            continue
        if compact.lower().startswith("page image"):
            continue
        paras.append(para)
    notes = ""
    notes_m = re.search(r"(?im)^##\s+(.+?\btasting notes?\b.*)$", chunk)
    if notes_m:
        # beschrijving = alinea's vóór de tasting-notes-kop
        before = chunk[:notes_m.start()]
        after = chunk[notes_m.end():]
        before_paras = []
        for raw in re.split(r"\n\s*\n", before):
            para = clean_text(raw)
            if not para:
                continue
            compact = re.sub(r"\s+", " ", para)
            if re.search(r"(?i)incl\.?\s*btw|direct leverbaar|gratis verzending|zondag voor|prijsklasse|€\d|\d+\.\d{2}\s+\d+", compact):
                continue
            if re.match(r"(?i)^(land|smaakprofiel|70cl|vol & rond)\b", compact):
                continue
            if len(compact) < 60 or compact.lower().startswith("page image"):
                continue
            before_paras.append(para)
        if before_paras:
            paras = before_paras
        note_txt = clean_text(after)
        note_txt = re.sub(r"(?is)\n###\s*Eigenschappen.*", "", note_txt).strip()
        if note_txt:
            notes = re.sub(r"\s+", " ", note_txt).strip()
    desc = "\n\n".join(paras).strip()
    return {"name": title, "abv": abv, "description": desc, "notes": notes}


def apply_fetched_markdown(items: list[dict], descriptions: dict, notes: dict, stats: Counter) -> None:
    """Lees opgeslagen WebFetch-markdown (DD/MoM/maker) en voeg zekere matches toe."""
    if not FETCHED_DIR.is_dir():
        return
    by_id = {it["id"]: it for it in items}
    def source_prio(rec: dict) -> int:
        host = host_of(rec.get("url") or "")
        if host == "drankdozijn.nl":
            return 0
        if host == "whisky.nl":
            return 1
        if host == "masterofmalt.com":
            return 9
        return 5

    fetched_files = []
    for path in FETCHED_DIR.glob("*.json"):
        rec = json.loads(path.read_text(encoding="utf-8"))
        fetched_files.append((source_prio(rec), path.name, path, rec))
    for _prio, _name, path, rec in sorted(fetched_files):
        url = rec.get("url") or ""
        md = rec.get("markdown") or rec.get("text") or ""
        item_id = rec.get("id")
        item = by_id.get(item_id)
        if not item:
            stats["fetched_unknown_id"] += 1
            continue
        host = host_of(url)
        parsed = {}
        if "masterofmalt.com" in host:
            parsed = parse_mom_markdown(md)
        elif "drankdozijn.nl" in host:
            parsed = parse_dd_markdown(md)
        else:
            # maker of overig: probeer markdown-alinea's
            parsed = parse_dd_markdown(md)  # zelfde alinea-filter is redelijk
            if not parsed.get("description"):
                parsed["description"] = strip_boilerplate(md)
        name = parsed.get("name") or ""
        cand_abv = parsed.get("abv") if ("%" in name or "abv" in name.lower()) else None
        if name and not confident_match(item["naam"], name, item.get("abv"), cand_abv):
            stats["fetched_mismatch"] += 1
            print("Geen zekere match: %s ↔ %s (%s)" % (item["naam"], name, url), file=sys.stderr)
            continue
        if not name and "masterofmalt.com" in host:
            # zonder titel geen zekere MoM-match
            stats["fetched_no_title"] += 1
            continue
        if parsed.get("description"):
            maybe_add_description(descriptions, item["id"], parsed["description"], url, stats)
        if parsed.get("notes"):
            maybe_add_notes(notes, item, parsed["notes"], url, stats)


def existing_dd_targets(items: list[dict], descriptions: dict) -> list[dict]:
    out = []
    for item in items:
        if item.get("omschrijving") or (descriptions.get(item["id"]) or {}).get("text"):
            continue
        for url in item_urls(item):
            if host_of(url) == "drankdozijn.nl":
                out.append({"id": item["id"], "naam": item["naam"], "url": url, "abv": item.get("abv")})
                break
    return out


def mom_url_guesses(name: str) -> list[str]:
    """Waarschijnlijke product-URL's op Master of Malt voor officiële core-range."""
    feat = features(name)
    folded = feat["folded"]
    # geen gok bij vatennummers of zeldzame IB-termen
    if feat["casks"]:
        return []
    ib_markers = {
        "signatory", "adelphi", "infrequent", "flyers", "carn", "mor",
        "lady", "glen", "douglas", "laing", "particular", "blackadder",
        "symington", "gordon", "macphail", "speymalt", "boutique",
        "cadenhead", "wemyss", "smws", "valinch", "mallet",
    }
    if set(feat["tokens"]) & ib_markers and "speymalt" not in feat["editions"]:
        # Speymalt is G&M maar vaak wél op MoM onder die naam; andere IB skippen
        if "speymalt" not in folded:
            return []
    tokens = [t for t in folded.split() if t not in {"cl", "sample", "ltr"} and not re.fullmatch(r"\d+(?:[.,]\d+)?", t)]
    # bouw slug
    slug_parts = []
    for t in tokens:
        if t in {"year", "years", "old", "jaar", "oud", "yo"}:
            continue
        slug_parts.append(t)
    if feat["age"] is not None:
        # zorg dat {age}-year-old in de slug zit
        base = []
        inserted_age = False
        for t in slug_parts:
            if t.isdigit() and int(t) == feat["age"] and not inserted_age:
                base.extend([t, "year", "old"])
                inserted_age = True
            else:
                if not (t.isdigit() and int(t) == feat["age"]):
                    base.append(t)
        if not inserted_age:
            base.extend([str(feat["age"]), "year", "old"])
        slug_parts = base
    slug = "-".join(slug_parts)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return []
    urls = [
        "https://www.masterofmalt.com/whiskies/%s-whisky/" % slug,
    ]
    # veel core-range pagina's: /whiskies/{distillery}/{slug}-whisky/
    if slug_parts:
        dist = slug_parts[0]
        if dist == "the" and len(slug_parts) > 1:
            dist = slug_parts[1]
        urls.append("https://www.masterofmalt.com/whiskies/%s/%s-whisky/" % (dist, slug))
        urls.append("https://www.masterofmalt.com/whiskies/%s/%s-year-old-whisky/" % (dist, dist + "-" + str(feat["age"]) if feat["age"] else slug))
    # uniek maken
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def write_fetch_queue(items: list[dict], descriptions: dict, matches: dict) -> None:
    ENRICH_DIR.mkdir(parents=True, exist_ok=True)
    dd = []
    seen = set()
    for rec in existing_dd_targets(items, descriptions):
        dd.append(rec)
        seen.add(rec["id"])
    for item_id, rec in (matches.get("drankdozijn.nl") or {}).items():
        if item_id in seen:
            continue
        item = next((it for it in items if it["id"] == item_id), None)
        if not item:
            continue
        if item.get("omschrijving") or (descriptions.get(item_id) or {}).get("text"):
            continue
        dd.append({"id": item_id, "naam": rec["naam"], "url": rec["url"], "abv": item.get("abv")})
    save_json(ENRICH_DIR / "queue-drankdozijn.json", dd)

    mom = []
    for item in items:
        if item.get("omschrijving") or (descriptions.get(item["id"]) or {}).get("text"):
            continue
        # bestaande proefnotities: alleen omschrijving zoeken, MoM blijft nuttig voor notes als notes ontbreken
        guesses = mom_url_guesses(item["naam"])
        if not guesses:
            continue
        mom.append({
            "id": item["id"],
            "naam": item["naam"],
            "abv": item.get("abv"),
            "score": item.get("score"),
            "urls": guesses,
        })
    # sorteer: eerst met score, hoogste eerst
    mom.sort(key=lambda r: (-(r["score"] or 0), r["naam"] or ""))
    save_json(ENRICH_DIR / "queue-masterofmalt.json", mom)
    print(
        "Wachtrij: Drankdozijn %s, Master of Malt %s (gok-URL's, nog te controleren)"
        % (len(dd), len(mom)),
        file=sys.stderr,
    )


def report(items: list[dict], descriptions: dict, notes: dict) -> None:
    have = sum(
        1 for it in items
        if it.get("omschrijving") or (descriptions.get(it["id"]) or {}).get("text")
    )
    src = Counter()
    for it in items:
        entry = descriptions.get(it["id"]) or {}
        text = it.get("omschrijving") or entry.get("text")
        url = it.get("omschrijvingBron") or entry.get("sourceUrl") or ""
        if text:
            src[source_label(url) or "onbekend"] += 1
    print("Items: %s  met omschrijving: %s  zonder: %s" % (len(items), have, len(items) - have))
    print("Omschrijvingen per bron:")
    for k, n in src.most_common():
        print("  %s: %s" % (k, n))
    print("Nieuwe/bestaande notities in notes.json: %s" % sum(1 for v in notes.values() if v.get("text")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Vul omschrijvingen en notities aan uit echte bronnen.")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--skip-wnl-fetch", action="store_true")
    args = parser.parse_args(argv)

    repo = args.repo.expanduser().resolve()
    items = load_items(repo)
    desc_path = repo / "descriptions.json"
    notes_path = repo / "notes.json"
    descriptions = load_descriptions(desc_path)
    notes = load_descriptions(notes_path)
    stats: Counter = Counter()

    print("Items geladen: %s, bestaande omschrijvingen in json: %s" % (
        len(items), sum(1 for v in descriptions.values() if v.get("text"))
    ), file=sys.stderr)

    apply_shopcache(items, descriptions, stats)
    print("Na shopcache: toegevoegd=%s  (per bron later in rapport)" % stats["added"], file=sys.stderr)

    matches = match_sitemaps(items, descriptions)
    save_json(ENRICH_DIR / "sitemap-matches.json", {
        "whisky.nl": len(matches["whisky.nl"]),
        "drankdozijn.nl": len(matches["drankdozijn.nl"]),
        "matches": matches,
    })
    print(
        "Sitemap-matches: whisky.nl=%s  drankdozijn=%s"
        % (len(matches["whisky.nl"]), len(matches["drankdozijn.nl"])),
        file=sys.stderr,
    )

    if not args.skip_wnl_fetch:
        fetch_whisky_nl(matches["whisky.nl"], descriptions, stats, args.delay)

    apply_fetched_markdown(items, descriptions, notes, stats)
    write_fetch_queue(items, descriptions, matches)

    save_json(desc_path, descriptions)
    save_json(notes_path, notes)
    print("Statistieken:", dict(stats), file=sys.stderr)
    report(items, descriptions, notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
