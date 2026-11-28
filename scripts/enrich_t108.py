#!/usr/bin/env python3
"""Vul ontbrekende omschrijvingen en proefnotities uit bestaande winkelcaches.

Alleen zekere matches (confident_match). Bestaande teksten worden niet
overschreven. Prijzen worden niet opgeslagen. Whiskybase wordt overgeslagen.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import enrich_sources as es

CACHE_DIRS = [
    Path("/workspace/whisky_build/shopcache"),
    Path("/workspace/whisky_build/wnlcache"),
    Path("/workspace/whisky_build/wscache"),
    Path("/workspace/whisky_build/wsearch"),
]
SKIP_HOSTS = {
    "whiskybase.com",
    "shop.whiskybase.com",
    "www.whiskybase.com",
}
NOTE_HEAD_RE = re.compile(
    r"(neus|smaak|afdronk|nasmaak|geur|mond|nose|palate|finish|aroma)\s*:",
    re.I,
)
NOTE_STRONG_RE = re.compile(
    r"<(?:strong|b|h[3-5]|span)[^>]*>\s*"
    r"(neus|smaak|afdronk|nasmaak|geur|mond|nose|palate|finish|aroma)\s*:?\s*"
    r"</(?:strong|b|h[3-5]|span)>",
    re.I,
)
TITLE_STRIP_RE = re.compile(
    r"\s+[|\-–—]\s*(Whiskysite\.nl.*|Whisky\.nl.*|['’]t Bockje.*|"
    r"kopen\s*\|\s*.*|World of Fine Spirits.*|Dramshop.*|"
    r"Woudenberg.*|Whisky Estate.*)$",
    re.I,
)
SAMPLE_URL_RE = re.compile(
    r"(3[\s-]?cl|6[\s-]?cl|sample|proefje|miniatuur)",
    re.I,
)
FLES_URL_RE = re.compile(r"(/fles/|flespagina|70\s*cl|70cl|/bottle/)", re.I)
DOT_EDITION_RE = re.compile(r"\b(\d{1,2}\.\d)\b(?!\s*%)")
AGE_TOKEN_RE = re.compile(r"^\d{1,2}(?:y|yo|j|jaar)$")
PAGE_CACHE = Path("/workspace/whisky_build/enrich/t108-pages.json")
# Extra tokens die een product niet uniek maken (bovenop STOPWORDS).
# Bottelaarsnamen staan hier ook: die mogen in de paginatitel voorkomen
# als ze bij het item als ib horen.
SOFT_EDITIONS = {"sherry", "bourbon"}
EXTRA_GENERIC = {
    "selection", "barrel", "finished", "finish", "matured", "special",
    "copy", "kopen", "webshop", "product", "70cl", "70ltr", "small",
    "limited", "distillery", "speyside", "islay", "highland", "lowland",
    "campbeltown", "islands", "island", "region", "series", "bottling",
    "original", "pure", "natural", "colour", "color", "ncf", "non",
    "chill", "filtered", "proof", "family", "collection", "batch",
    "single", "cask", "strength", "oak", "ex", "first", "fill", "refill",
    "hogshead", "butt", "barrel", "quarter", "puncheon", "barrique",
}
BOTTLER_TOKS = {
    "signatory", "vintage", "adelphi", "claxtons", "claxton", "blackadder",
    "infrequent", "flyers", "maltman", "ultimate", "carn", "mor",
    "gordon", "macphail", "macphail", "douglas", "laing", "particular",
    "north", "star", "spirits", "murray", "mcdavid", "fragrant", "drops",
    "living", "souls", "chieftains", "chieftain", "berry", "bros", "rudd",
    "hart", "brothers", "james", "eadie", "goldfinch", "hidden",
    "coopers", "cooper", "choice", "uncharted", "company", "smws",
    "cadenhead", "cadenheads", "samaroli", "willow", "jack", "that",
    "boutique", "y",
}


def dotted_editions(name: str) -> set[str]:
    # Alleen punt-edities (14.2), geen ABV met komma (57,7%).
    return set(DOT_EDITION_RE.findall(name or ""))


def distinctive_tokens(name: str, abv=None) -> tuple[set[str], dict]:
    feat = es.features(name, abv)
    toks = set(feat["tokens"])
    for t in es.brand_head(feat["tokens"]):
        toks.discard(t)
    toks -= EXTRA_GENERIC
    toks -= BOTTLER_TOKS
    toks = {t for t in toks if not AGE_TOKEN_RE.match(t)}
    return toks, feat


def tight_match(item_name: str, cand_name: str, item_abv=None, cand_abv=None, item_ib: str = "") -> bool:
    """Strengere check bovenop confident_match: editie, vat en unieke woorden."""
    if not es.confident_match(item_name, cand_name, item_abv, cand_abv):
        return False
    a_toks, a = distinctive_tokens(item_name, item_abv)
    b_toks, b = distinctive_tokens(cand_name, cand_abv)
    a_dots, b_dots = dotted_editions(item_name), dotted_editions(cand_name)
    if a_dots or b_dots:
        if a_dots != b_dots:
            return False
    folded_b = b["folded"]
    folded_a = a["folded"]
    ib_fold = es.fold(item_ib or "")
    for t in a_toks:
        if t not in folded_b and t not in b_toks:
            return False
    for t in b_toks:
        if t in folded_a or t in a_toks:
            continue
        if t in BOTTLER_TOKS and t in ib_fold:
            continue
        if t in ib_fold and len(t) > 3:
            continue
        return False
    extra_b = b["editions"] - a["editions"] - SOFT_EDITIONS
    extra_a = a["editions"] - b["editions"] - SOFT_EDITIONS
    if extra_a or extra_b:
        return False
    # Verschillende onafhankelijke bottelaars zijn nooit dezelfde fles.
    a_bot = bottler_phrases(item_name, item_ib)
    b_bot = bottler_phrases(cand_name, "")
    if a_bot and b_bot and a_bot.isdisjoint(b_bot):
        return False
    if a_bot and not b_bot:
        # paginatitel noemt de bottelaar niet; alleen oké als de ib-naam
        # ook niet in de itemnaam zit als extra eis — te onzeker bij IB.
        if es.fold(item_ib or "") and not any(p in es.fold(item_name) for p in a_bot):
            pass
        elif a_bot and not any(p in es.fold(cand_name) for p in a_bot):
            # item noemt bottelaar in de naam, pagina niet
            if any(p in es.fold(item_name) for p in a_bot):
                return False
    return True


BOTTLER_PHRASES = (
    "signatory vintage", "signatory", "adelphi", "claxtons", "claxton",
    "blackadder", "infrequent flyers", "the maltman", "maltman",
    "the ultimate", "carn mor", "gordon and macphail", "gordon macphail",
    "douglas laing", "old particular", "north star", "murray mcdavid",
    "fragrant drops", "living souls", "james eadie", "berry bros",
    "hart brothers", "goldfinch", "hidden spirits", "coopers choice",
    "cooper s choice", "uncharted", "cadenhead", "samaroli", "smws",
    "that boutique", "elevenses", "lady of the glen", "little brown dog",
    "whiskynerds", "whisky nerds", "dramfool", "the whisky agency",
    "whisky mercenary", "elixir", "first cask", "ferg and harris",
    "ferg harris", "chieftains", "bottle dreams",
)


def bottler_phrases(name: str, ib: str = "") -> set[str]:
    folded = es.fold(" ".join(x for x in (name, ib) if x))
    found = set()
    for phrase in BOTTLER_PHRASES:
        if phrase in folded:
            found.add(phrase)
    # normaliseer varianten
    if "claxton" in found or "claxtons" in found:
        found.update({"claxton", "claxtons"})
    return found


BOILERPLATE_RE = re.compile(
    r"gekozen op smaak|smaakvierkant|inschatting, geen oordeel",
    re.I,
)


def is_real_notes(text: str) -> bool:
    if not text or BOILERPLATE_RE.search(text):
        return False
    if not NOTE_HEAD_RE.search(text):
        return False
    return len(re.sub(r"\s+", " ", text).strip()) >= 40


def is_real_description(desc: str, *names: str) -> bool:
    """True alleen bij lopende tekst, niet bij een herhaalde producttitel."""
    text = es.strip_boilerplate(desc or "")
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return False
    name_words = set(es.fold(" ".join(n for n in names if n)).split())
    leftover_words = []
    for w in es.fold(compact).split():
        if w in name_words:
            continue
        if re.match(r"^\d", w):
            continue
        if w in {"cl", "ml", "ltr", "l", "lees", "meer", "sample", "percent", "vol"}:
            continue
        leftover_words.append(w)
    leftover = " ".join(leftover_words)
    if len(leftover) < 80:
        return False
    return es.is_useful_description(compact)


def is_sample_url(url: str) -> bool:
    path = urlparse(url).path
    return bool(SAMPLE_URL_RE.search(path))


def page_rank(url: str) -> int:
    """Hogere score = liever gebruiken. Flespagina's gaan voor samples."""
    score = 0
    if FLES_URL_RE.search(url) and not is_sample_url(url):
        score += 20
    if is_sample_url(url):
        score -= 10
    host = es.host_of(url)
    # prefer NL shops we were asked to use
    prefer = {
        "bockje.nl": 8,
        "whisky.nl": 7,
        "whiskysite.nl": 5,
        "drankgigant.nl": 4,
        "whiskyestate.nl": 4,
        "dramshop.nl": 3,
        "woudenbergdranken.nl": 3,
    }
    score += prefer.get(host, 1)
    return score


def clean_title(name: str) -> str:
    name = es.clean_text(name or "")
    name = TITLE_STRIP_RE.sub("", name).strip()
    name = re.sub(r"\s+[|\-–—]\s*$", "", name).strip()
    return name


def extract_notes(html_text: str) -> str:
    if not html_text or not (NOTE_STRONG_RE.search(html_text) or NOTE_HEAD_RE.search(html_text)):
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup.select("script, style, noscript, nav, footer, header"):
        tag.decompose()

    best = ""
    for el in soup.find_all(["p", "div", "section", "article", "li"]):
        raw = str(el)
        if el.find(["p", "div", "section", "article"]) and not NOTE_STRONG_RE.search(
            "".join(str(c) for c in el.contents if getattr(c, "name", None) not in {"div", "section", "article"})
        ):
            # skip huge wrappers; prefer inner blocks
            if el.name in {"div", "section", "article"} and el.find(["div", "section", "article", "p"]):
                continue
        text = el.get_text("\n", strip=True)
        if not NOTE_HEAD_RE.search(text):
            continue
        heads = set(m.group(1).lower() for m in NOTE_HEAD_RE.finditer(text))
        # require at least one tasting-section head, prefer 2+
        useful = heads & {"neus", "smaak", "afdronk", "nasmaak", "nose", "palate", "finish", "geur", "mond"}
        if not useful:
            continue
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) < 40:
            continue
        # drop obvious wrappers that include the whole product page
        if len(compact) > 2500:
            continue
        score = len(useful) * 1000 + len(compact)
        # prefer blocks that start near a heading
        if re.match(r"(neus|smaak|nose|palate|proefnotitie|tasting)\b", compact, re.I):
            score += 200
        if score > (len(re.findall(r"(neus|smaak|afdronk|nose|palate|finish)\s*:", best, re.I)) * 1000 + len(best)):
            best = text
    if not best:
        return ""
    # trim leading volume/cask-only lines if notes start later
    lines = [ln.strip() for ln in best.splitlines() if ln.strip()]
    start = 0
    for i, ln in enumerate(lines):
        if NOTE_HEAD_RE.search(ln):
            start = i
            break
    best = "\n".join(lines[start:])
    return es.clean_text(best)


def load_cache_pages(force: bool = False) -> list[dict]:
    if PAGE_CACHE.is_file() and not force:
        pages = json.loads(PAGE_CACHE.read_text(encoding="utf-8"))
        for page in pages:
            page["rank"] = page_rank(page["url"])
        return pages
    pages = []
    seen_urls = set()
    for cache_dir in CACHE_DIRS:
        if not cache_dir.is_dir():
            continue
        for url_path in cache_dir.glob("*.url"):
            url = url_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if not url:
                continue
            url = url[0].strip()
            if not url.startswith("http"):
                continue
            host = es.host_of(url)
            if not host or host in SKIP_HOSTS or "whiskybase." in host:
                continue
            if url in seen_urls:
                continue
            html_path = url_path.with_suffix(".html")
            if not html_path.is_file():
                continue
            seen_urls.add(url)
            html_text = html_path.read_text(encoding="utf-8", errors="replace")
            if len(html_text) < 500:
                continue
            name, desc = es.extract_shop_description(html_text, url)
            name = clean_title(name)
            notes = extract_notes(html_text)
            if not name:
                continue
            if not es.is_useful_description(desc):
                desc = ""
            if notes and len(re.sub(r"\s+", " ", notes)) < 40:
                notes = ""
            if not desc and not notes:
                continue
            pages.append({
                "url": url,
                "name": name,
                "abv": es.parse_abv(name),
                "description": desc,
                "notes": notes,
                "rank": page_rank(url),
                "host": host,
            })
    PAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(PAGE_CACHE, pages)
    return pages


def index_items_by_brand(items: list[dict]) -> dict:
    by = defaultdict(list)
    for item in items:
        feat = es.features(item["naam"], item.get("abv"))
        key = tuple(es.brand_head(feat["tokens"]))
        if key:
            by[key].append(item)
    return by


def candidates_for(page: dict, by_brand: dict, items_by_id: dict) -> list[dict]:
    feat = es.features(page["name"], page.get("abv"))
    key = tuple(es.brand_head(feat["tokens"]))
    return by_brand.get(key) or []


def apply_caches(repo: Path, dry_run: bool = False) -> dict:
    items = es.load_items(repo)
    # load_items uses compact+details; also merge existing desc/notes files
    desc_path = repo / "descriptions.json"
    notes_path = repo / "notes.json"
    descriptions = es.load_descriptions(desc_path)
    notes = es.load_descriptions(notes_path)
    items_by_id = {it["id"]: it for it in items}

    pages = load_cache_pages()
    by_brand = index_items_by_brand(items)
    stats = Counter()
    stats["pages_with_text"] = len(pages)
    report = {
        "added_desc": [],
        "added_notes": [],
        "skipped": [],
    }

    # Collect best match per item+field
    best_desc: dict[str, tuple[int, dict]] = {}
    best_notes: dict[str, tuple[int, dict]] = {}

    for page in pages:
        cands = candidates_for(page, by_brand, items_by_id)
        stats["page_cand_sum"] += len(cands)
        matched = False
        for item in cands:
            if not tight_match(item["naam"], page["name"], item.get("abv"), page.get("abv"), item.get("ib") or ""):
                continue
            matched = True
            item_id = item["id"]
            rank = page["rank"]
            if page["description"] and is_real_description(page["description"], page["name"], item.get("naam") or ""):
                prev = best_desc.get(item_id)
                if not prev or rank > prev[0] or (rank == prev[0] and len(page["description"]) > len(prev[1]["description"])):
                    best_desc[item_id] = (rank, page)
            if page["notes"] and is_real_notes(page["notes"]):
                prev = best_notes.get(item_id)
                if not prev or rank > prev[0] or (rank == prev[0] and len(page["notes"]) > len(prev[1]["notes"])):
                    best_notes[item_id] = (rank, page)
        if not matched:
            stats["pages_unmatched"] += 1

    for item_id, (_, page) in best_desc.items():
        if es.maybe_add_description(descriptions, item_id, page["description"], page["url"], stats, items_by_id[item_id].get("naam")):
            item = items_by_id[item_id]
            report["added_desc"].append({
                "id": item_id,
                "naam": item.get("naam"),
                "url": page["url"],
                "host": page["host"],
                "samplePage": is_sample_url(page["url"]),
            })

    # Pagina zonder ABV die op meerdere flessen met verschillende ABV past: overslaan.
    notes_by_url = defaultdict(list)
    for item_id, (rank, page) in best_notes.items():
        notes_by_url[page["url"]].append(item_id)
    ambiguous = set()
    for url, ids in notes_by_url.items():
        if len(ids) < 2:
            continue
        abvs = set()
        for iid in ids:
            try:
                abvs.add(round(float(items_by_id[iid].get("abv")), 2))
            except (TypeError, ValueError):
                abvs.add(None)
        page_abv = best_notes[ids[0]][1].get("abv")
        if page_abv is None and len(abvs) > 1:
            ambiguous.update(ids)
            stats["notes_ambiguous_abv"] += len(ids)

    for item_id, (_, page) in best_notes.items():
        if item_id in ambiguous:
            continue
        item = items_by_id[item_id]
        if es.maybe_add_notes(notes, item, page["notes"], page["url"], stats):
            report["added_notes"].append({
                "id": item_id,
                "naam": item.get("naam"),
                "url": page["url"],
                "host": page["host"],
                "samplePage": is_sample_url(page["url"]),
            })

    if not dry_run:
        es.save_json(desc_path, descriptions)
        es.save_json(notes_path, notes)

    report["stats"] = dict(stats)
    report["dry_run"] = dry_run
    return report


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    repo = Path(__file__).resolve().parent.parent
    report = apply_caches(repo, dry_run=dry)
    out = Path("/workspace/whisky_build/enrich/t108-cache-report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(out, report)
    s = report["stats"]
    print(
        "Pagina's met tekst: %s  nieuwe omschrijvingen: %s  nieuwe notities: %s  "
        "al aanwezig desc/notes: %s/%s  ongeëvenaard: %s%s"
        % (
            s.get("pages_with_text", 0),
            s.get("added", 0),
            s.get("notes_added", 0),
            s.get("already_had", 0),
            s.get("notes_already", 0),
            s.get("pages_unmatched", 0),
            "  [dry-run]" if dry else "",
        )
    )
    by_host_d = Counter()
    by_host_n = Counter()
    for rec in report["added_desc"]:
        by_host_d[rec["host"]] += 1
    for rec in report["added_notes"]:
        by_host_n[rec["host"]] += 1
    if by_host_d:
        print("Omschrijvingen per bron:", dict(by_host_d))
    if by_host_n:
        print("Notities per bron:", dict(by_host_n))
    print("Rapport:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
