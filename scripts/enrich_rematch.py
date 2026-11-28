#!/usr/bin/env python3
"""Extra matchronde: Whiskysite-flesfeiten als extra sleutels.

Voor whisky's die nog een gat hebben en een strikte Whiskysite-flesmatch,
worden de flesgegevens (volle naam, leeftijd, ABV, jaartal, bottelaar,
vatnummer) als extra sleutels gebruikt om opnieuw te matchen tegen
Drankdozijn, Drankgigant, Gall & Gall, Whiskybase Shop, Drankenhandel Leiden,
Dramshop en de bottelaarsites. Alleen strikte matches. Bestaande waarden
worden nooit overschreven. Whiskysite-feiten winnen. Master of Malt blijft
alleen informatiebron. Geen Cloudflare-omzeiling, robots.txt en crawl-delay
worden gerespecteerd. Niets in match-review.json wordt goedgekeurd.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

import enrich_dd
import enrich_drankgigant as edg
import enrich_extra as ex
import enrich_gall
import enrich_sources as es
import enrich_wsfles as wsfles
import fetch_flesprijzen as ff
import sources as src

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path("/workspace/whisky_build")
REPORT_DIR = BUILD / "gaten"
REPORT = REPORT_DIR / "rematch-report.json"
GALL_SITEMAP = BUILD / "gallcache" / "sitemap_0-product.xml"
DD_CATALOG = BUILD / "prijzen" / "cache" / "catalogus-drankdozijn.json"
DG_CATALOG = BUILD / "prijzen" / "cache" / "catalogus-drankgigant.json"

USER_AGENT = ff.USER_AGENT
# Conservatieve pauzes (minstens de crawl-delay uit robots.txt).
DELAYS = {
    "drankdozijn": 1.0,
    "drankgigant": 1.0,
    "gall": 1.0,
    "dramshop": 2.0,
    "whiskybaseshop": 2.0,
    "drankenhandelleiden": 2.0,
    "gordonmacphail": 2.0,
    "douglaslaing": 1.0,
    "northstar": 1.0,
    "cadenhead": 2.0,
}

SHOP_SOURCES = ("dramshop", "whiskybaseshop", "drankenhandelleiden")
MAKER_SOURCES = ("gordonmacphail", "douglaslaing", "northstar", "cadenhead")
ALL_REMATCH = SHOP_SOURCES + MAKER_SOURCES + ("drankdozijn", "drankgigant", "gall")

# Volume gelezen als vatnummer: "Sherry Cask 0,70 ltr".
FAKE_CASK_RE = re.compile(r"^(0+|70|75|50|20|10|5|3)$")
GALL_SKU_RE = re.compile(r"-(\d{5,})$")
YEAR_LINE_RE = re.compile(
    r"(?i)\b(?:distilled|gedistilleerd|vintage)\b[^\n]{0,20}\b(19[5-9]\d|20[0-2]\d)\b"
)
AGE_LINE_RE = re.compile(
    r"(?i)(?:^|\n)\s*(\d{1,2})\s*(?:years?\s*old|year\s*old|jaar(?:\s*oud)?)\s*(?:\n|$)"
)


def gap(item: dict) -> bool:
    has_d = bool(item.get("omschrijving"))
    has_n = bool(item.get("proefnotities") or item.get("notities"))
    has_i = str(item.get("afbeelding") or "").startswith("http")
    return not (has_d and has_n and has_i)


def real_casks(name: str) -> set[str]:
    out = set()
    for c in ex.casks_of(name):
        if not c or FAKE_CASK_RE.fullmatch(c):
            continue
        out.add(c)
    return out



def catalog_conflict(name_a: str, name_b: str) -> list[str]:
    """Tegenstrijdige leeftijd, jaartal, batch, vat of editienummer in catalogustitels."""
    a, b = es.features(name_a), es.features(name_b)
    issues = []
    if a["age"] and b["age"] and a["age"] != b["age"]:
        issues.append("leeftijd")
    ya, yb = a["vintages"] | a["release_years"], b["vintages"] | b["release_years"]
    if ya and yb and ya.isdisjoint(yb):
        issues.append("jaartal")
    if a["batches"] and b["batches"] and a["batches"].isdisjoint(b["batches"]):
        issues.append("batch")
    ac, bc = real_casks(name_a), real_casks(name_b)
    if ac and bc and ac.isdisjoint(bc):
        issues.append("vatnummer")
    def ednums(s: str) -> set[str]:
        return set(re.findall(r"\b(\d+\.\d+)\b", s or ""))
    ea, eb = ednums(name_a), ednums(name_b)
    if ea and eb and ea.isdisjoint(eb):
        issues.append("nummer")
    return issues


def bottle_facts(info: dict) -> dict:
    """Feiten uit titel, specs en duidelijk gelabelde stub-regels (geen marketingtekst)."""
    title = ex.composed_name(info) or info.get("titel") or ""
    blob_title = title
    feat = es.features(blob_title, ex.page_abv(info))
    desc = info.get("omschrijving") or ""
    # Alleen korte, gelabelde regels uit de stub (niet de hele marketingtekst).
    stub_lines = []
    for line in desc.splitlines():
        s = line.strip()
        if not s or len(s) > 80:
            continue
        if AGE_LINE_RE.search("\n" + s + "\n") or YEAR_LINE_RE.search(s) or ex.CASKNR_RE.search(s):
            stub_lines.append(s)
    stub = " ".join(stub_lines)
    feat_stub = es.features(stub) if stub else None
    age = feat["age"]
    if age is None and feat_stub:
        age = feat_stub["age"]
    vintages = set(feat["vintages"])
    if feat_stub:
        vintages |= feat_stub["vintages"]
    for m in YEAR_LINE_RE.finditer(desc):
        vintages.add(m.group(1))
    casks = real_casks(title) | real_casks(stub)
    bottler = ex.bottler_of(title)
    return {
        "titel": title,
        "age": age,
        "abv": ex.page_abv(info),
        "vintages": vintages,
        "casks": casks,
        "bottler": bottler,
        "url": info.get("url"),
    }


def consistent(item: dict, facts: dict) -> list[str]:
    """Tegenstrijdigheden tussen sample en flespagina (leeg = bruikbaar)."""
    name = ff.strip_sample(item["naam"])
    a = es.features(name, item.get("abv"))
    issues = []
    if a["age"] and facts["age"] and a["age"] != facts["age"]:
        issues.append("leeftijd %s vs %s" % (a["age"], facts["age"]))
    ia, pa = es.parse_abv(item.get("abv")), facts["abv"]
    if ia is not None and pa is not None and abs(ia - pa) > ff.ABV_TOLERANCE:
        issues.append("ABV %s vs %s" % (ia, pa))
    ac = real_casks(name)
    if ac and facts["casks"] and ac.isdisjoint(facts["casks"]):
        issues.append("vatnummer %s vs %s" % (ac, facts["casks"]))
    if a["vintages"] and facts["vintages"] and a["vintages"].isdisjoint(facts["vintages"]):
        issues.append("jaartal %s vs %s" % (a["vintages"], facts["vintages"]))
    # Editienummers zoals 14.2 / 15.2
    def ednums(s: str) -> set[str]:
        return set(re.findall(r"\b(\d+\.\d+)\b", s or ""))

    ea, eb = ednums(name), ednums(facts["titel"])
    if ea and eb and ea.isdisjoint(eb):
        issues.append("nummer %s vs %s" % (ea, eb))
    ib = item.get("ib") or ex.item_bottler(item)
    if ib and facts["bottler"] and ib != facts["bottler"]:
        issues.append("bottelaar %s vs %s" % (ib, facts["bottler"]))
    return issues


def enrich_item(item: dict, facts: dict) -> dict:
    """Kopie van het item met flesfeiten als extra sleutels in de naam."""
    name = ff.strip_sample(item["naam"])
    a = es.features(name, item.get("abv"))
    extras = []
    if facts["age"] and not a["age"]:
        extras.append("%d Years Old" % facts["age"])
    if facts["vintages"] and not a["vintages"]:
        extras.extend(sorted(facts["vintages"]))
    ac = real_casks(name)
    if facts["casks"] and not ac:
        extras.extend("Cask %s" % c for c in sorted(facts["casks"]))
    ib = item.get("ib") or ex.item_bottler(item)
    if facts["bottler"] and not ib:
        extras.append(facts["bottler"])
        ib = facts["bottler"]
    bname = facts["titel"]
    ia = es.parse_abv(item.get("abv"))
    # Volle flesnaam alleen als die dezelfde whisky aanduidt.
    if bname and (
        es.confident_match(name, bname, ia, ia)
        or ff.names_equivalent(name, bname)
    ):
        new_name = bname
    else:
        new_name = (name + " " + " ".join(extras)).strip()
    fake = dict(item)
    fake["naam"] = new_name
    fake["_oorspronkelijkeNaam"] = item["naam"]
    abv = ia if ia is not None else facts["abv"]
    if abv is not None:
        fake["abv"] = abv
    if facts["age"] and not item.get("leeftijd"):
        fake["leeftijd"] = facts["age"]
    if ib:
        fake["ib"] = ib
    return fake


def target_items(repo: Path) -> list[tuple[dict, dict, dict]]:
    """(origineel item, flesinfo, verrijkt item) voor gaten met strikte flesmatch."""
    items = {it["id"]: it for it in es.load_items(repo)}
    acc, _rev, _st = wsfles.matches(repo)
    out = []
    for iid, (_sc, info, _how) in acc.items():
        item = items.get(iid)
        if not item or not gap(item):
            continue
        facts = bottle_facts(info)
        if consistent(item, facts):
            continue
        out.append((item, info, enrich_item(item, facts)))
    return out


def load_json_list(path: Path) -> list:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def gall_catalog() -> list[dict]:
    """Productnamen uit de Gall-sitemap-slugs (geen extra request)."""
    if not GALL_SITEMAP.is_file():
        return []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(GALL_SITEMAP)
    cat = []
    for loc in tree.findall(".//s:loc", ns):
        url = (loc.text or "").strip()
        if not url.startswith("https://www.gall.nl/"):
            continue
        from urllib.parse import unquote
        slug = unquote(urlparse(url).path.strip("/").rsplit(".", 1)[0])
        slug = GALL_SKU_RE.sub("", slug)
        name = slug.replace("-", " ").strip()
        if not name:
            continue
        cat.append({"url": url, "name": name})
    return cat


def cached_info(source: str, url: str) -> dict | None:
    if source in ex.ALL_SOURCES:
        path = ex.cache_file(source, url)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    if source == "drankgigant":
        path = edg.cache_file(url)
        if path.is_file():
            rec = json.loads(path.read_text(encoding="utf-8"))
            if not rec.get("titel"):
                rec["titel"] = rec.get("naam") or rec.get("name") or ""
            if not rec.get("specs"):
                rec["specs"] = {"abv": rec.get("abv"), "inhoud": rec.get("grootte") or "70 cl"}
            return rec
    if source == "drankdozijn":
        # pagina's staan per item-id; zoek op url
        for path in (BUILD / "enrich" / "dd-pages").glob("*.json"):
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec.get("url") == url:
                return {
                    "url": url,
                    "titel": rec.get("name") or "",
                    "omschrijving": rec.get("description") or "",
                    "notities": rec.get("notes") or "",
                    "afbeelding": rec.get("image") or "",
                    "specs": {
                        "abv": rec.get("abv"),
                        "leeftijd": rec.get("age"),
                        "inhoud": rec.get("size") or "70 cl",
                    },
                }
        # fetch_flesprijzen cache
        try:
            path = ff.page_cache_path("drankdozijn", url) if hasattr(ff, "page_cache_path") else None
        except Exception:
            path = None
        if path and Path(path).is_file():
            rec = json.loads(Path(path).read_text(encoding="utf-8"))
            return rec if rec.get("titel") or rec.get("name") else None
    if source == "gall":
        path = enrich_gall.page_path(url)
        if path.is_file():
            page = enrich_gall.parse_page(path.read_text(encoding="utf-8", errors="replace"), url)
            return {
                "url": url,
                "titel": page.get("name") or "",
                "omschrijving": page.get("description") or "",
                "notities": enrich_gall.notes_from_page(page) if hasattr(enrich_gall, "notes_from_page") else (page.get("notes") or ""),
                "afbeelding": page.get("image") or "",
                "specs": {
                    "abv": page.get("abv"),
                    "leeftijd": page.get("age"),
                    "inhoud": page.get("size") or page.get("inhoud") or "70 cl",
                },
            }
    return None


def differences_for(source: str, item: dict, info: dict) -> list[str]:
    if source in ex.ALL_SOURCES:
        return ex.differences(item, info, source)
    if source == "drankgigant":
        ok, why = edg.accept_info(item, info)
        return [] if ok else [why or "geen zekere match"]
    # DD / Gall: confident_match + ABV + leeftijd, zoals hun apply
    name = info.get("titel") or info.get("name") or ""
    pa = ex.page_abv(info) if info.get("specs") else es.parse_abv(info.get("abv"))
    if info.get("specs") and (info["specs"].get("abv") not in (None, "")):
        pa = es.parse_abv(info["specs"]["abv"]) if not isinstance(info["specs"]["abv"], (int, float)) else float(info["specs"]["abv"])
    ia = es.parse_abv(item.get("abv"))
    diffs = []
    if name and not (es.confident_match(item["naam"], name, ia, pa) or ff.names_equivalent(item["naam"], name)):
        diffs.append("naam afwijkend (geen zekere naam-match)")
    if ia is not None and pa is not None and abs(ia - pa) > ff.ABV_TOLERANCE:
        diffs.append("ABV %s vs %s" % (ia, pa))
    a = es.features(ff.strip_sample(item["naam"]), ia)
    b = es.features(name, pa)
    if a["age"] and b["age"] and a["age"] != b["age"]:
        diffs.append("leeftijd %s vs %s" % (a["age"], b["age"]))
    elif b["age"] and not a["age"]:
        diffs.append("pagina noemt leeftijd %s" % b["age"])
    elif a["age"] and not b["age"] and str(a["age"]) not in (b.get("folded") or "").split():
        diffs.append("leeftijd %s niet op pagina" % a["age"])
    if a["vintages"] and b["vintages"] and a["vintages"].isdisjoint(b["vintages"]):
        diffs.append("jaartal %s vs %s" % (a["vintages"], b["vintages"]))
    ac, bc = real_casks(item["naam"]), real_casks(name)
    if ac and bc and ac.isdisjoint(bc):
        diffs.append("vatnummer %s vs %s" % (ac, bc))
    titel = name
    if ff.BAD_TITLE_RE.search(titel) and not re.search(r"(?i)\b70\s*cl\b", titel):
        diffs.append("geen reguliere fles")
    return diffs


def catalog_hits(fake: dict, source: str, idx, catalog=None) -> list[tuple[float, dict]]:
    if source in ex.ALL_SOURCES:
        return ex.candidates(fake, idx, source, top=5)
    # DD / DG / Gall: eerst strikte catalogusmatch, anders een losse score voor review
    hits = []
    if idx is not None:
        for rec in ff.candidates_for(fake, idx):
            hits.append((1.0, rec))
    if hits:
        return hits
    # Geen strikte catalogustreffer: toch de beste losse kandidaten bekijken
    # (alleen als we een pagina hebben of willen ophalen). Zelfde drempel als extra.
    name = ff.strip_sample(fake["naam"])
    feat = es.features(name, fake.get("abv"))
    seen, scored = set(), []
    for tok in feat["tokens"][:3]:
        for rec in (idx or {}).get(tok, []):
            if rec["url"] in seen:
                continue
            seen.add(rec["url"])
            if not es.same_brand(name, rec["name"]):
                continue
            if not ff.same_bottler(fake, rec["name"]):
                continue
            sc = 0.0
            b = rec.get("_feat") or es.features(rec["name"])
            overlap = len(feat["token_set"] & b["token_set"])
            union = len(feat["token_set"] | b["token_set"]) or 1
            sc = overlap / union
            if feat["age"] and b.get("age") == feat["age"]:
                sc += 0.3
            if feat["vintages"] and b.get("vintages") and not feat["vintages"].isdisjoint(b["vintages"]):
                sc += 0.3
            if feat["casks"] and real_casks(rec["name"]) and not feat["casks"].isdisjoint(real_casks(rec["name"])):
                sc += 0.5
            if catalog_conflict(name, rec["name"]):
                continue
            if sc >= 0.85:
                scored.append((sc, rec))
    scored.sort(key=lambda x: -x[0])
    return scored[:3]


def build_indexes() -> dict:
    indexes = {}
    for source in SHOP_SOURCES + MAKER_SOURCES:
        path = ex.CACHE / ("catalogus-%s.json" % source)
        if not path.is_file():
            continue
        cat = json.loads(path.read_text(encoding="utf-8"))
        if source in ex.HUBSPOT:
            indexes[source] = {"_catalog": cat, "_cat": cat}
        else:
            idx = ex.build_index(cat)
            idx["_cat"] = cat
            indexes[source] = idx
    for key, path, shop in (
        ("drankdozijn", DD_CATALOG, "drankdozijn"),
        ("drankgigant", DG_CATALOG, "drankgigant"),
        ("gall", None, "gall"),
    ):
        if key == "gall":
            cat = gall_catalog()
        else:
            cat = load_json_list(path)
        if not cat:
            continue
        idx = ff.build_index(cat)
        idx["_cat"] = cat
        indexes[key] = idx
    return indexes


def search(repo: Path, targets: list, indexes: dict) -> dict:
    """Zoek strikte en bijna-matches in de catalogi (cache first)."""
    accepted = []  # dicts
    missing_pages = []  # te ophalen (source, url, rec, fake, item)
    near = []
    stats = Counter()
    seen_accept = set()
    for item, info, fake in targets:
        stats["doelen"] += 1
        for source, idx in indexes.items():
            hits = catalog_hits(fake, source, idx)
            stats[source + ":kandidaten"] += len(hits)
            best = None
            for sc, rec in hits:
                url = rec["url"]
                page = cached_info(source, url)
                if not page:
                    missing_pages.append({
                        "id": item["id"],
                        "naam": item["naam"],
                        "verrijkt": fake["naam"],
                        "bron": source,
                        "url": url,
                        "catalogusNaam": rec.get("name"),
                        "score": sc,
                    })
                    stats[source + ":niet_opgehaald"] += 1
                    continue
                diffs = differences_for(source, fake, page)
                if not diffs:
                    key = (item["id"], url)
                    if key not in seen_accept:
                        if best is None or sc > best["score"]:
                            best = {
                                "id": item["id"],
                                "naam": item["naam"],
                                "verrijkt": fake["naam"],
                                "bron": source,
                                "url": url,
                                "titel": page.get("titel") or rec.get("name"),
                                "score": sc,
                                "info": page,
                                "flesUrl": info.get("url"),
                            }
                elif ex.near_match(diffs) if source in ex.ALL_SOURCES else len(diffs) <= 2:
                    near.append({
                        "id": item["id"],
                        "naam": item["naam"],
                        "verrijkt": fake["naam"],
                        "bron": source,
                        "url": url,
                        "titel": page.get("titel") or rec.get("name"),
                        "verschil": diffs,
                        "score": sc,
                    })
            if best:
                seen_accept.add((best["id"], best["url"]))
                accepted.append(best)
                stats[source + ":strikt"] += 1
    return {
        "accepted": accepted,
        "missing_pages": missing_pages,
        "near": near,
        "stats": dict(stats),
    }


def robots_delay(session: requests.Session, host: str, default: float) -> float:
    """Lees crawl-delay uit robots.txt; bij twijfel de hogere pauze."""
    url = "https://%s/robots.txt" % host
    try:
        r = session.get(url, timeout=20)
        text = r.text or ""
        if r.status_code != 200 or _looks_blocked(text, r.status_code) or "<html" in text[:200].lower():
            # 403/challenge: niet omzeilen
            if r.status_code != 200 or _looks_blocked(text, r.status_code) or "controleren" in text.lower() or "just a moment" in text.lower():
                return None
            return default
        delay = default
        # Geen extra paden ophalen als we Disallow: / hebben voor * .
        ua = None
        our = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("user-agent:"):
                ua = line.split(":", 1)[1].strip()
                our = ua == "*"
                continue
            if our and low.startswith("crawl-delay:"):
                try:
                    delay = max(delay, float(line.split(":", 1)[1].strip()))
                except ValueError:
                    pass
            if our and low.startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path == "/":
                    return None  # niet ophalen
        return delay
    except requests.RequestException:
        return default


HOST_OF = {
    "drankdozijn": "drankdozijn.nl",
    "drankgigant": "www.drankgigant.nl",
    "gall": "www.gall.nl",
    "dramshop": "www.dramshop.nl",
    "whiskybaseshop": "shop.whiskybase.com",
    "drankenhandelleiden": "www.drankenhandelleiden.nl",
    "gordonmacphail": "www.gordonandmacphail.com",
    "douglaslaing": "www.douglaslaing.com",
    "northstar": "northstarspirits.com",
    "cadenhead": "www.cadenhead.scot",
}


def fetch_missing(missing: list[dict], max_pages: int = 0) -> Counter:
    """Haal ontbrekende kandidaatpagina's op, met robots.txt en crawl-delay."""
    stats = Counter()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept-Language"] = "nl-NL,nl;q=0.9,en;q=0.8"
    # Unieke URL's, beste score eerst, max 2 per whisky. Alleen hoge score
    # zonder catalogusconflict (batch/jaar/editie).
    by_id = defaultdict(list)
    for rec in sorted(missing, key=lambda r: -r["score"]):
        if rec["score"] < 0.9:
            continue
        if catalog_conflict(rec.get("verrijkt") or rec.get("naam") or "", rec.get("catalogusNaam") or ""):
            continue
        by_id[rec["id"]].append(rec)
    planned = []
    seen = set()
    for iid, recs in by_id.items():
        for rec in recs[:2]:
            if rec["url"] in seen:
                continue
            seen.add(rec["url"])
            planned.append(rec)
    if max_pages:
        planned = planned[:max_pages]
    delays = {}
    blocked = set()
    # Drankdozijn staat achter een Cloudflare-check ("Even controleren"); niet omzeilen.
    # Drankgigant geeft vanaf deze omgeving een TLS-EOF; niet forceren.
    blocked.update({"drankdozijn", "drankgigant"})
    for source in {r["bron"] for r in planned}:
        if source in blocked:
            print("sla %s over (Cloudflare of TLS-fout; geen omzeiling)" % source, file=sys.stderr)
            stats[source + ":overgeslagen"] += 1
            continue
        host = HOST_OF.get(source)
        if not host:
            blocked.add(source)
            continue
        d = robots_delay(session, host, DELAYS.get(source, 2.0))
        if d is None:
            print("robots.txt verbiedt %s, sla over" % source, file=sys.stderr)
            blocked.add(source)
            stats[source + ":robots_blok"] += 1
        else:
            delays[source] = d
            print("crawl-delay %s: %s s" % (source, d), file=sys.stderr)
    last_time: dict[str, float] = {}
    for n, rec in enumerate(planned, 1):
        source, url = rec["bron"], rec["url"]
        if source in blocked:
            continue
        # Anti-bot / Cloudflare: nooit whiskybase.com (de database); shop is oké.
        if "whiskybase.com" in url and "shop.whiskybase.com" not in url:
            stats["overgeslagen_whiskybase"] += 1
            continue
        wait = delays.get(source, 2.0)
        prev = last_time.get(source)
        if prev is not None:
            gap_s = time.time() - prev
            if gap_s < wait:
                time.sleep(wait - gap_s)
        try:
            ok = _fetch_one(session, source, url, rec)
        except requests.RequestException as exc:
            print("fout %s: %s" % (url, exc), file=sys.stderr)
            stats[source + ":fout"] += 1
            last_time[source] = time.time()
            continue
        last_time[source] = time.time()
        stats[source + (":ok" if ok else ":fout")] += 1
        if n % 20 == 0:
            print("rematch ophalen %s/%s" % (n, len(planned)), file=sys.stderr)
    stats["gepland"] = len(planned)
    return stats


def _looks_blocked(text: str, status: int) -> bool:
    if status in (403, 429, 503):
        return True
    low = (text or "")[:4000].lower()
    signs = (
        "attention required",
        "just a moment",
        "cf-browser-verification",
        "cloudflare",
        "one moment, please",
        "access denied",
        "checking your browser",
    )
    return any(s in low for s in signs) and len(text) < 20000


def _fetch_one(session: requests.Session, source: str, url: str, rec: dict) -> bool:
    if source in ex.LIGHTSPEED:
        info = ex.lightspeed_info(source, url, session)
        if info.get("fout"):
            return False
        ex.cache_file(source, url).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return True
    if source in ex.SHOPIFY:
        # Shopify: catalogusrecord heeft al de productdata; shopify_info gebruikt rec.
        return False
    if source in ex.WOO:
        info = ex.woo_info(source, url, session)
        if info.get("fout"):
            return False
        ex.cache_file(source, url).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return True
    if source in ex.HUBSPOT:
        info = ex.gm_info(source, url, session)
        if info.get("fout"):
            return False
        ex.cache_file(source, url).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return True
    resp = session.get(url, timeout=25)
    if _looks_blocked(resp.text, resp.status_code):
        print("anti-bot, niet omzeilen: %s (%s)" % (url, resp.status_code), file=sys.stderr)
        return False
    if resp.status_code != 200:
        return False
    if source == "drankgigant":
        info = edg.parse_info_page(resp.text, url)
        edg.cache_file(url).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return True
    if source == "drankdozijn":
        # hergebruik enrich_dd parser indien aanwezig, anders een minimale JSON-LD/parse
        info = _parse_dd(resp.text, url)
        path = BUILD / "enrich" / "dd-pages" / ("rematch-%s.json" % _slug(url))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return True
    if source == "gall":
        path = enrich_gall.page_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
        return True
    return False


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", urlparse(url).path.strip("/").lower())[:80].strip("-")


def _parse_dd(html: str, url: str) -> dict:
    """Minimale Drankdozijn-pagina (JSON-LD), zonder data te verzinnen."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    name, desc, image, abv, notes = "", "", "", None, ""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (TypeError, ValueError):
            continue
        nodes = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            nodes = data["@graph"]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            typ = node.get("@type")
            if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                name = name or (node.get("name") or "")
                desc = desc or (node.get("description") or "")
                img = node.get("image")
                if isinstance(img, list) and img:
                    img = img[0]
                if isinstance(img, dict):
                    img = img.get("url")
                if isinstance(img, str):
                    image = image or img
    titel = name or (soup.title.string if soup.title else "") or ""
    text = "%s %s" % (titel, desc)
    m = ex.TEXT_ABV_RE.search(text)
    if m:
        abv = float(m.group(1).replace(",", "."))
    return {
        "url": url,
        "name": titel,
        "titel": titel,
        "description": desc,
        "omschrijving": desc,
        "notes": notes,
        "image": image,
        "abv": abv,
        "age": (es.features(titel).get("age")),
        "specs": {"abv": abv, "inhoud": "70 cl", "leeftijd": es.features(titel).get("age")},
    }


def apply_accepted(repo: Path, accepted: list[dict]) -> dict:
    """Vul alleen lege velden uit strikte rematch-treffers. Nooit overschrijven."""
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    facts = es.load_descriptions(repo / "facts.json") if (repo / "facts.json").is_file() else {}
    items = {it["id"]: it for it in es.load_items(repo)}
    stats = Counter()
    report = {"added_desc": [], "added_notes": [], "added_images": [], "added_facts": [], "matches": []}
    # Cascadevolgorde voor omschrijving.
    def rank(bron: str) -> int:
        key = {
            "gall": "gall",
            "drankdozijn": "drankdozijn",
            "drankgigant": "drankgigant",
        }.get(bron, bron)
        return src.rank(key, "omschrijving")

    grouped = defaultdict(list)
    for rec in accepted:
        grouped[rec["id"]].append(rec)
    for iid, hits in grouped.items():
        item = items[iid]
        hits = sorted(hits, key=lambda h: rank(h["bron"]))
        for rec in hits:
            source, url, info = rec["bron"], rec["url"], rec["info"]
            report["matches"].append({
                "id": iid, "naam": item["naam"], "bron": source,
                "url": url, "titel": rec.get("titel"), "verrijkt": rec.get("verrijkt"),
            })
            text = info.get("omschrijving") or info.get("description") or ""
            source_key = {
                "gall": "gall",
                "drankdozijn": "drankdozijn",
                "drankgigant": "drankgigant",
            }.get(source, source)
            if src.may_fill(source_key, "omschrijving") and text:
                have = (item.get("omschrijving") or (descriptions.get(iid) or {}).get("text") or "").strip()
                if not es.is_useful_description(have):
                    if es.maybe_add_description(descriptions, iid, text, url, stats, item.get("naam")):
                        stats[source + ":omschrijving"] += 1
                        report["added_desc"].append({"id": iid, "naam": item["naam"], "url": url})
            note = info.get("notities") or info.get("notes") or ""
            if source in ex.ALL_SOURCES:
                note = ex.page_notes(info) or note
            if src.may_fill(source_key, "proefnotities") and note and es.maybe_add_notes(notes, item, note, url, stats):
                stats[source + ":proefnotities"] += 1
                report["added_notes"].append({"id": iid, "naam": item["naam"], "url": url})
            img = info.get("afbeelding") or info.get("image") or ""
            entry = descriptions.get(iid) or {}
            has_img = str(entry.get("image") or item.get("afbeelding") or "").startswith("https://")
            if src.may_fill(source_key, "afbeelding") and str(img).startswith("https://") and not has_img:
                e = dict(entry)
                e.update({"image": img, "imageSource": source, "imagePage": url})
                descriptions[iid] = e
                stats[source + ":afbeelding"] += 1
                report["added_images"].append({"id": iid, "naam": item["naam"], "image": img, "url": url})
            vat = (info.get("specs") or {}).get("vattype")
            fact = dict(facts.get(iid) or {})
            vat_empty = not item.get("vattype") or str(item["vattype"]).lower().startswith("onbekend")
            if src.may_fill(source_key, "feiten") and vat and vat_empty and not fact.get("vattype"):
                fact["vattype"] = str(vat).strip()
                fact["sourceUrl"] = fact.get("sourceUrl") or url
                facts[iid] = fact
                stats[source + ":feit_vattype"] += 1
                report["added_facts"].append({"id": iid, "naam": item["naam"], "velden": {"vattype": vat}, "url": url})
            stats[source + ":toegepast"] += 1
    es.save_json(repo / "descriptions.json", descriptions)
    es.save_json(repo / "notes.json", notes)
    es.save_json(repo / "facts.json", facts)
    report["stats"] = dict(stats)
    return report


# --------------------------------------------------------------------------
# match-review herscore
# --------------------------------------------------------------------------

LABEL_SOURCE = {
    "Dramshop.nl": "dramshop",
    "Whiskybase Shop": "whiskybaseshop",
    "Drankenhandel Leiden": "drankenhandelleiden",
    "Drankgigant": "drankgigant",
    "Gordon & MacPhail": "gordonmacphail",
    "Douglas Laing": "douglaslaing",
    "North Star Spirits": "northstar",
    "Cadenhead": "cadenhead",
    "Whiskysite.nl (fles)": "whiskysitefles",
    "Drankdozijn": "drankdozijn",
    "Gall & Gall": "gall",
}


def rescore_review(repo: Path, targets_by_id: dict) -> dict:
    """Herscore bestaande kandidaten met flesfeiten. Keur niets goed."""
    path = repo / "data" / "match-review.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("kandidaten") or []
    became_strict = []
    still_null = 0
    skipped = 0
    no_page = 0
    updated = []
    for r in rows:
        # Nooit een bestaande goedkeuring of afwijzing aanpassen.
        if r.get("goedgekeurd") is not None:
            updated.append(r)
            skipped += 1
            continue
        item_id = r["id"]
        if item_id not in targets_by_id:
            updated.append(r)
            still_null += 1
            continue
        _item, _info, fake = targets_by_id[item_id]
        source = LABEL_SOURCE.get(r["bron"])
        page = None
        if source == "whiskysitefles":
            path_c = wsfles.cache_file(r["kandidaatUrl"])
            if path_c.is_file():
                page = json.loads(path_c.read_text(encoding="utf-8"))
        elif source:
            page = cached_info(source, r["kandidaatUrl"])
        if not page:
            no_page += 1
            updated.append(r)
            still_null += 1
            continue
        if source == "whiskysitefles":
            diffs = ex.differences(fake, page, "dramshop")  # zelfde Lightspeed-check, bron alleen voor maat
            # forceer geen bottelaar-bronregels
        else:
            diffs = differences_for(source, fake, page)
        if not diffs:
            became_strict.append({
                "id": item_id,
                "whisky": r["whisky"],
                "bron": r["bron"],
                "kandidaatNaam": r["kandidaatNaam"],
                "kandidaatUrl": r["kandidaatUrl"],
                "was": r.get("verschil"),
                "verrijkt": fake["naam"],
            })
            # Niet in de review laten staan: het is nu een strikte match, geen kandidaat.
            # goedgekeurd blijft ongemoeid (we zetten het niet op true).
            continue
        new = dict(r)
        new["verschil"] = diffs
        new["goedgekeurd"] = None
        updated.append(new)
        still_null += 1
    data["kandidaten"] = updated
    # schrijven gebeurt pas bij --apply / --write-review, niet in droge ronde
    return {
        "became_strict": became_strict,
        "remaining_null": still_null,
        "skipped_beslist": skipped,
        "no_page": no_page,
        "remaining_rows": updated,
        "payload": data,
        "was_count": len(rows),
        "now_count": len(updated),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--dry-run", action="store_true", help="Alleen rapporteren, niets schrijven of ophalen.")
    ap.add_argument("--fetch", action="store_true", help="Ontbrekende kandidaatpagina's ophalen (robots/crawl-delay).")
    ap.add_argument("--apply", action="store_true", help="Strikte matches toepassen en review herschrijven (goedgekeurd blijft null).")
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args(argv)
    repo = args.repo
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    targets = target_items(repo)
    print("Doelen (gat + strikte flesmatch, consistent): %s" % len(targets), file=sys.stderr)
    indexes = build_indexes()
    print("Catalogi: %s" % {k: len(v.get("_cat") or v.get("_catalog") or []) for k, v in indexes.items()}, file=sys.stderr)
    found = search(repo, targets, indexes)
    print("Strikt (cache): %s  ontbrekende pagina's: %s  bijna: %s" % (
        len(found["accepted"]), len(found["missing_pages"]), len(found["near"])
    ), file=sys.stderr)
    print("Stats:", found["stats"], file=sys.stderr)
    for rec in found["accepted"]:
        print("  STRIKT %s [%s] %s → %s" % (rec["naam"][:50], rec["bron"], rec["verrijkt"][:40], rec.get("titel", "")[:50]), file=sys.stderr)

    fetch_stats = {}
    if args.fetch and found["missing_pages"]:
        # Alleen hoge-score ontbrekende pagina's
        fetch_stats = dict(fetch_missing(found["missing_pages"], args.max_pages))
        print("Opgehaald:", fetch_stats, file=sys.stderr)
        found = search(repo, targets, indexes)
        print("Na ophalen strikt: %s  ontbreekt: %s" % (len(found["accepted"]), len(found["missing_pages"])), file=sys.stderr)

    targets_by_id = {item["id"]: (item, info, fake) for item, info, fake in targets}
    review = rescore_review(repo, targets_by_id)
    print("Review herscore: %s → strikt, %s blijven null (was %s kandidaten, nu %s)" % (
        len(review["became_strict"]), review["remaining_null"], review["was_count"], review["now_count"]
    ), file=sys.stderr)
    for rec in review["became_strict"]:
        print("  REVIEW-STRIKT %s [%s] was %s" % (rec["whisky"][:50], rec["bron"], rec["was"]), file=sys.stderr)

    apply_report = {}
    if args.apply:
        # Voeg review-strikt toe als we de pagina hebben en nog niet in accepted
        have = {(r["id"], r["url"]) for r in found["accepted"]}
        for rec in review["became_strict"]:
            source = LABEL_SOURCE.get(rec["bron"])
            if not source or source == "whiskysitefles":
                continue
            key = (rec["id"], rec["kandidaatUrl"])
            if key in have:
                continue
            page = cached_info(source, rec["kandidaatUrl"])
            if not page:
                continue
            item = targets_by_id[rec["id"]][0]
            found["accepted"].append({
                "id": rec["id"],
                "naam": item["naam"],
                "verrijkt": rec["verrijkt"],
                "bron": source,
                "url": rec["kandidaatUrl"],
                "titel": rec["kandidaatNaam"],
                "score": 1.0,
                "info": page,
            })
            have.add(key)
        apply_report = apply_accepted(repo, found["accepted"])
        # match-review.json blijft ongewijzigd: niets goedkeuren. De herscore
        # staat in het rapport (hoeveel strikte hits, hoeveel blijven null).
        print("Toegepast:", apply_report["stats"], file=sys.stderr)
        print("Review herscore (niet goedgekeurd, bestand ongewijzigd): %s strikt, %s blijven null, %s kandidaten." % (
            len(review["became_strict"]), review["remaining_null"], review["was_count"]
        ), file=sys.stderr)

    out = {
        "doelen": len(targets),
        "search_stats": found["stats"],
        "accepted": [{k: v for k, v in r.items() if k != "info"} for r in found["accepted"]],
        "missing_pages": found["missing_pages"],
        "near": found["near"],
        "review_became_strict": review["became_strict"],
        "review_remaining_null": review["remaining_null"],
        "review_was": review["was_count"],
        "review_now": review["now_count"],
        "fetch_stats": fetch_stats,
        "apply": {k: apply_report[k] for k in apply_report if k != "stats"} | {"stats": apply_report.get("stats")} if apply_report else {},
    }
    es.save_json(REPORT, out)
    print("Rapport:", REPORT, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
