#!/usr/bin/env python3
"""Haal informatie (geen prijzen) van Drankgigant.nl: omschrijving, proefnotities, kenmerken en foto.

Dezelfde strikte match als de flesprijzen (`fetch_flesprijzen.py`): dezelfde naam,
bottelaar, leeftijd, editie en vat, ABV binnen 0,3 en dezelfde flesmaat. Samples,
miniaturen en cadeausets worden overgeslagen. Voorraad speelt voor informatie geen rol.

Volgens de cascade (`sources.py`) vult Drankgigant alleen LEGE velden voor omschrijving,
afbeelding en feiten; proefnotities komen er altijd bij naast die van andere bronnen.
Bestaande waarden worden nooit overschreven. Teksten zijn letterlijk, met bron-URL.

Gebruik
  python scripts/enrich_drankgigant.py --fetch     # pagina's ophalen (beleefd, met cache)
  python scripts/enrich_drankgigant.py --apply     # cache verwerken in descriptions/notes/facts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString

import enrich_sources as es
import fetch_flesprijzen as ff
import sources as src

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path("/workspace/whisky_build/dgcache/pages")
REPORT = Path("/workspace/whisky_build/dgcache/apply-report.json")
SOURCE = "drankgigant"
PLACEHOLDER_RE = re.compile(r"(?i)placeholder|no[_-]?image|geen[_-]?afbeelding")


def cache_file(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def block_text(node) -> str:
    """Tekst met alinea-structuur: <br> en blokelementen worden regelovergangen."""
    for tag in node.find_all(["style", "script"]):
        tag.decompose()
    for br in node.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for tag in node.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr"]):
        tag.append(NavigableString("\n\n" if tag.name in ("p", "ul", "ol", "table") else "\n"))
    return node.get_text("")


def parse_info_page(html: str, url: str) -> dict:
    """Naam, omschrijving, kenmerken, ABV, maat en foto van één Drankgigant-productpagina."""
    info = {"url": url, "fout": None}
    prods = ff.ld_products(html)
    if not prods:
        info["fout"] = "geen JSON-LD product"
        return info
    prod = prods[0]
    info["naam"] = prod.get("name")
    soup = BeautifulSoup(html, "lxml")
    desc_node = soup.select_one(".description-pdp")
    info["omschrijving"] = block_text(desc_node) if desc_node else ""
    # Kopregel 'Beschrijving' staat in sommige pagina's boven de tekst; alleen die hele regel verdwijnt.
    info["omschrijving"] = re.sub(r"^\s*Beschrijving\s*\n+", "", info["omschrijving"])
    if not info["omschrijving"] and prod.get("description"):
        info["omschrijving"] = str(prod["description"])
    attrs: dict[str, str] = {}
    table = soup.select_one(".additional-attributes.big-view") or soup.select_one("#product-attributes")
    if table:
        for label in table.select(".product-attribute-label"):
            value = label.find_next_sibling(class_="product-attribute-value")
            if value:
                key = label.get_text(" ", strip=True)
                val = value.get_text(" ", strip=True)
                if key and val and key not in attrs:
                    attrs[key] = val
    info["kenmerken"] = attrs
    info["abv"] = ff.parse_float(attrs.get("Alcoholpercentage"))
    info["grootte"] = ff.size_label(attrs.get("Inhoud")) or ff.size_label(info.get("naam"))
    image = prod.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, str) and image.startswith("http") and not PLACEHOLDER_RE.search(image):
        info["afbeelding"] = image.split("?")[0]
    return info


def accept_info(item: dict, info: dict) -> tuple[bool, str]:
    """Zelfde strikte match als ff.accept, maar zonder voorraad en prijs."""
    if info.get("fout"):
        return False, info["fout"]
    want = item.get("flesGrootte") or ff.DEFAULT_SIZE
    if info.get("grootte") != ff.size_label(want):
        return False, "flesmaat %s" % info.get("grootte")
    if info.get("abv") is None or item.get("abv") in (None, ""):
        return False, "ABV onbekend"
    if abs(float(item["abv"]) - float(info["abv"])) > ff.ABV_TOLERANCE:
        return False, "ABV verschilt"
    if ff.BAD_TITLE_RE.search(info.get("naam") or ""):
        return False, "geen reguliere fles"
    name = ff.strip_sample(item["naam"])
    if not es.confident_match(name, info.get("naam") or "", item.get("abv"), info["abv"]):
        return False, "naam wijkt af"
    if not ff.names_equivalent(name, info.get("naam") or ""):
        return False, "naam wijkt af"
    return True, ""


def candidate_pages(repo: Path, session=None) -> tuple[dict, dict]:
    """Koppel items aan Drankgigant-URL's (zekere naam-match); geeft (by_url, stats)."""
    session = session or requests.Session()
    session.headers["User-Agent"] = ff.USER_AGENT
    cat_path = ff.CACHE_DIR / "catalogus-drankgigant.json"
    if cat_path.is_file() and time.time() - cat_path.stat().st_mtime < 72 * 3600:
        catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    else:
        catalog = ff.load_drankgigant_catalog(session)
        ff.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cat_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    idx = ff.build_index(catalog)
    by_url: dict[str, list[dict]] = defaultdict(list)
    stats = Counter(catalogus=len(catalog))
    for item in es.load_items(repo):
        if not item.get("naam"):
            continue
        hits = ff.candidates_for(item, idx)
        urls = {h["url"] for h in hits}
        if len(urls) == 1:
            by_url[hits[0]["url"]].append(item)
        elif len(urls) > 1:
            stats["dubbelzinnig"] += 1
            if len({es.fold(h["name"]) for h in hits}) == 1:
                by_url[hits[0]["url"]].append(item)
    stats["kandidaat_paginas"] = len(by_url)
    return by_url, stats


def fetch_pages(repo: Path, delay: float, max_pages: int = 0) -> Counter:
    session = requests.Session()
    session.headers["User-Agent"] = ff.USER_AGENT
    by_url, stats = candidate_pages(repo, session)
    urls = sorted(by_url)
    if max_pages:
        urls = urls[:max_pages]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for n, url in enumerate(urls, 1):
        path = cache_file(url)
        if path.is_file():
            stats["cache"] += 1
            continue
        try:
            resp = ff.http_get(session, url)
        except requests.RequestException:
            stats["fout_http"] += 1
            continue
        time.sleep(delay)
        if resp.status_code != 200:
            stats["fout_http"] += 1
            continue
        info = parse_info_page(resp.text, url)
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        stats["opgehaald"] += 1
        if n % 50 == 0:
            print("drankgigant: %s/%s pagina's" % (n, len(urls)), file=sys.stderr)
    return stats


def field_empty(item: dict, field: str) -> bool:
    val = item.get(field)
    return val is None or val == "" or str(val).strip().lower().startswith("onbekend")


def has_image(item: dict, entry: dict | None) -> bool:
    if entry and str(entry.get("image") or "").startswith("https://"):
        return True
    return str(item.get("afbeelding") or "").startswith("https://")


def notes_from_info(text: str, attrs: dict) -> str:
    """Neus/Smaak/Afdronk letterlijk: expliciete secties, anders de zinnen met neus/smaak/afdronk."""
    formatted = src.format_description(text)
    explicit = src.explicit_note_sections(formatted)
    if explicit:
        return explicit
    bits = []
    for sent in re.split(r"(?<=[.!?])\s+", re.sub(r"\s*\n+\s*", " ", formatted)):
        if re.search(r"(?i)\b(neus|geur|aroma|smaak|proeft|afdronk|tonen van|hints van)\b", sent):
            bits.append(sent.strip())
    return "\n".join(dict.fromkeys(bits))


def clean_heading(text: str) -> str:
    return re.sub(r"^\s*Beschrijving\s*\n+", "", text or "")


def apply_cache(repo: Path, descriptions: dict, notes: dict, facts: dict, items_by_id: dict | None = None) -> dict:
    """Verwerk gecachete pagina's volgens de cascade. Muteert descriptions, notes en facts."""
    items = es.load_items(repo)
    items_by_id = items_by_id or {it["id"]: it for it in items}
    by_url, stats = candidate_pages(repo)
    report = {"added_desc": [], "added_notes": [], "added_images": [], "added_facts": [], "afgewezen": []}
    for url, group in sorted(by_url.items()):
        path = cache_file(url)
        if not path.is_file():
            stats["niet_opgehaald"] += 1
            continue
        info = json.loads(path.read_text(encoding="utf-8"))
        for item in group:
            ok, why = accept_info(item, info)
            if not ok:
                stats["afgewezen"] += 1
                report["afgewezen"].append({"id": item["id"], "naam": item["naam"], "pagina": info.get("naam"), "url": url, "reden": why})
                continue
            stats["gematcht"] += 1
            iid = item["id"]
            entry = descriptions.get(iid)
            info["omschrijving"] = clean_heading(info.get("omschrijving"))
            if src.may_fill(SOURCE, "omschrijving") and info.get("omschrijving"):
                # Een omschrijving die uit een eerdere Drankgigant-run komt telt niet als bestaand.
                own = "" if src.source_key(item.get("omschrijvingBron") or "") == SOURCE else (item.get("omschrijving") or "")
                have = (own or (entry or {}).get("text") or "").strip()
                if not es.is_useful_description(have):
                    if es.maybe_add_description(descriptions, iid, info["omschrijving"], url, stats):
                        stats["omschrijving"] += 1
                        report["added_desc"].append({"id": iid, "naam": item["naam"], "url": url})
            if src.may_fill(SOURCE, "proefnotities"):
                note = notes_from_info(info.get("omschrijving") or "", info.get("kenmerken") or {})
                if note and es.maybe_add_notes(notes, item, note, url, stats):
                    stats["proefnotities"] += 1
                    report["added_notes"].append({"id": iid, "naam": item["naam"], "url": url})
            image = info.get("afbeelding")
            if src.may_fill(SOURCE, "afbeelding") and image and not has_image(item, descriptions.get(iid)):
                e = dict(descriptions.get(iid) or {})
                e["image"] = image
                e["imageSource"] = SOURCE
                e["imagePage"] = url
                descriptions[iid] = e
                stats["afbeelding"] += 1
                report["added_images"].append({"id": iid, "naam": item["naam"], "image": image, "url": url})
            if src.may_fill(SOURCE, "feiten"):
                attrs = info.get("kenmerken") or {}
                fact = dict(facts.get(iid) or {})
                added = {}
                # Streek van Drankgigant volgt een eigen indeling (bijv. Aberlour als Highland) en
                # wordt niet overgenomen. Leeftijd alleen als het getal ook in de naam staat.
                age = attrs.get("Leeftijd in jaren")
                if (
                    age and str(age).isdigit() and field_empty(item, "leeftijd") and not fact.get("leeftijd")
                    and re.search(r"\b%s\s*(?:years?|jaar|yo|y\.?o\.?)\b" % int(age), item.get("naam") or "", re.I)
                ):
                    added["leeftijd"] = int(age)
                if added:
                    fact.update(added)
                    fact["sourceUrl"] = fact.get("sourceUrl") or url
                    facts[iid] = fact
                    stats["feiten"] += len(added)
                    report["added_facts"].append({"id": iid, "naam": item["naam"], "velden": added, "url": url})
    report["stats"] = dict(stats)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args(argv)
    if args.fetch:
        print("Ophalen:", dict(fetch_pages(args.repo, args.delay, args.max_pages)), file=sys.stderr)
    if args.apply:
        repo = args.repo
        descriptions = es.load_descriptions(repo / "descriptions.json")
        notes = es.load_descriptions(repo / "notes.json")
        facts = es.load_descriptions(repo / "facts.json")
        report = apply_cache(repo, descriptions, notes, facts)
        es.save_json(repo / "descriptions.json", descriptions)
        es.save_json(repo / "notes.json", notes)
        if facts:
            es.save_json(repo / "facts.json", facts)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        es.save_json(REPORT, report)
        print(report["stats"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
