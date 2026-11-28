#!/usr/bin/env python3
"""Whiskysite-flespagina's als informatiebron, direct na de samplepagina.

fetch_descriptions.py keek alleen naar een flesLink die al in de data stond, en
liet een korte (maar niet volledig 'generieke') sampletekst voorgaan op de
flespagina. Dit script zoekt in de sitemap van whiskysite.nl naar de volle fles
(70 cl of de flesmaat van de whisky), met dezelfde strikte match als de andere
bronnen, en vult alleen lege velden. Is de sampletekst een stub en de flespagina
een volledige omschrijving, dan gaat die flespagina voor (ook boven Drankgigant).
De bron blijft Whiskysite.nl, met de URL van de flespagina.

Gebruik
  python scripts/enrich_wsfles.py --fetch     # sitemap + kandidaatpagina's (cache, crawl-delay 2 s)
  python scripts/enrich_wsfles.py --apply     # cache verwerken in descriptions/notes/facts
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
from bs4 import BeautifulSoup

import enrich_extra as ex
import enrich_sources as es
import fetch_descriptions as fd
import fetch_flesprijzen as ff
import fetch_images as fi
import sources as src

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path("/workspace/whisky_build/wsfles")
WS_HTML = Path("/workspace/whisky_build/wscache")
SITEMAP = "https://www.whiskysite.nl/nl/sitemap.xml"
SITEMAP_CACHE = WS_HTML / "sitemap-nl.xml"
SOURCE = "whiskysitefles"
SHOP_ID = 39103
DELAY = 2.0
REPORT = CACHE / "apply-report.json"

# Slug of URL die (vrijwel) zeker een sample is; ingekorte slugs als '...-606-6.html'
# controleren we pas op de echte titel.
SAMPLE_SLUG_RE = re.compile(
    r"(?i)(?:^|-)((?:3|5|6|10)-?cl(?:-sample)?|sample|proeffles|proefset|miniatuur|miniature)(?:-|\.|$)"
)
# Ingekorte slug van een sample, bijv. "...-59-6-c.html" of "...-606-6.html".
TRUNCATED_SAMPLE_RE = re.compile(r"(?i)-\d{2,3}-[36](?:-c)?(?:-|\.|$)")
BOTTLE_SLUG_RE = re.compile(r"(?i)0?70-ltr|70-cl|100-ltr|0?75-ltr")
ID_SUFFIX_RE = re.compile(r"-(\d{8,})$")


def session() -> requests.Session:
    return ex.session()


def cache_file(url: str) -> Path:
    return CACHE / "pages" / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def load_sitemap(s: requests.Session | None = None) -> list[str]:
    if SITEMAP_CACHE.is_file() and time.time() - SITEMAP_CACHE.stat().st_mtime < 72 * 3600:
        text = SITEMAP_CACHE.read_text(encoding="utf-8", errors="replace")
    else:
        s = s or session()
        resp = ff.http_get(s, SITEMAP)
        resp.raise_for_status()
        text = resp.text
        SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SITEMAP_CACHE.write_text(text, encoding="utf-8")
    return re.findall(r"<loc>(https://www\.whiskysite\.nl/nl/[^<]+)</loc>", text)


def slug_name(url: str) -> tuple[str, str | None, float | None]:
    """Ruwe naam, maat en ABV uit de slug, alleen voor de voorselectie."""
    slug = re.sub(r"\.html$", "", url.rsplit("/", 1)[-1])
    slug = ID_SUFFIX_RE.sub("", slug)
    size = None
    for pat, label in (
        (r"(?:^|-)(0?70)-ltr(?:-|$)", "70 cl"),
        (r"(?:^|-)(70)-cl(?:-|$)", "70 cl"),
        (r"(?:^|-)(100)-ltr(?:-|$)", "100 cl"),
        (r"(?:^|-)(0?75)-ltr(?:-|$)", "75 cl"),
        (r"(?:^|-)(50)-cl(?:-|$)", "50 cl"),
    ):
        if re.search(pat, slug):
            size = label
            slug = re.sub(pat, "-", slug)
            break
    abv = None
    m = re.search(r"-(\d{2,3})$", slug)
    if m:
        raw = m.group(1)
        val = int(raw) / (10 if len(raw) == 3 else 1)
        if 35 <= val <= 75:
            abv = val
            slug = slug[: m.start()]
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    name = slug.replace("-", " ").strip()
    if size:
        name += " " + size
    if abv is not None:
        name += " %s%%" % (("%g" % abv).replace(".", ","))
    return name, size, abv


def is_sample_slug(url: str) -> bool:
    path = url.rsplit("/", 1)[-1]
    if SAMPLE_SLUG_RE.search(path):
        return True
    # Alleen als de slug geen duidelijke flesmaat heeft.
    if BOTTLE_SLUG_RE.search(url):
        return False
    return bool(TRUNCATED_SAMPLE_RE.search(path))


def load_catalog(s: requests.Session | None = None) -> list[dict]:
    path = CACHE / "catalogus-whiskysite-fles.json"
    if path.is_file() and time.time() - path.stat().st_mtime < 72 * 3600:
        return json.loads(path.read_text(encoding="utf-8"))
    out = []
    for url in load_sitemap(s):
        if not url.endswith(".html") or is_sample_slug(url):
            continue
        name, size, abv = slug_name(url)
        out.append({"url": url, "name": name, "size": size, "abv": abv})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def parse_json_page(data: dict, url: str) -> dict:
    p = data.get("product") or {}
    title = ex._html.unescape(p.get("fulltitle") or p.get("title") or "")
    content = ex.html_text(p.get("content") or "")
    short = ex._html.unescape(p.get("description") or "").strip()
    text = content if len(content) >= len(short) else short
    info = {
        "url": url,
        "bron": SOURCE,
        "fout": None,
        "titel": title,
        "omschrijving": text,
        "specs": {},
        "specsRaw": {},
    }
    size = ff.size_label(title) or ff.size_label(text)
    if size:
        info["specs"]["inhoud"] = size
    if info.get("titel"):
        pa = ex.page_abv(info)
        if pa:
            info["specs"]["abv"] = ("%g" % pa).replace(".", ",") + "%"
    img = p.get("image")
    if img:
        info["afbeelding"] = "https://cdn.webshopapp.com/shops/%s/files/%s/image.jpg" % (SHOP_ID, img)
    return info


def parse_html_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = fi.extract_title(html) if hasattr(fi, "extract_title") else ""
    if not title:
        h1 = soup.select_one("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        if not title:
            og = soup.find("meta", attrs={"property": re.compile(r"^og:title$", re.I)})
            title = (og.get("content") or "") if og else ""
    text = fd.extract_description(html) or ""
    info = {
        "url": url,
        "bron": SOURCE,
        "fout": None,
        "titel": title,
        "omschrijving": text,
        "specs": {},
        "specsRaw": {},
    }
    size = ff.size_label(title) or ff.size_label(text)
    if size:
        info["specs"]["inhoud"] = size
    pa = ex.page_abv(info)
    if pa:
        info["specs"]["abv"] = ("%g" % pa).replace(".", ",") + "%"
    img = fi.extract_image(html) or fd.extract_image(html)
    if img:
        info["afbeelding"] = fi.upgrade_ws_image(img)
    return info


def info_ok(info: dict) -> tuple[bool, str]:
    if info.get("fout"):
        return False, info["fout"]
    title = info.get("titel") or ""
    url = info.get("url") or ""
    if fi.is_sample_product(title, url) or ff.BAD_TITLE_RE.search(title):
        return False, "geen reguliere fles"
    size = ex.page_size(info)
    if size and size in ("3 cl", "5 cl", "6 cl", "10 cl"):
        return False, "samplemaat %s" % size
    return True, ""


def existing_ws_bottle_url(item: dict) -> str | None:
    """flesLink of tweedeLink op Whiskysite die geen sample is."""
    for field in ("flesLink", "tweedeLink"):
        url = item.get(field) or ""
        if fd.is_whiskysite(url) and not fi.is_sample_product(None, url) and not is_sample_slug(url):
            return url
    for rec in item.get("flesMeer") or []:
        url = rec.get("link") or rec.get("url") or ""
        if fd.is_whiskysite(url) and not fi.is_sample_product(None, url) and not is_sample_slug(url):
            return url
    return None


def candidates_for(item: dict, idx: dict) -> list[tuple[float, dict]]:
    hits = [(sc, rec) for sc, rec in ex.candidates(item, idx, "whiskysite", top=8) if not is_sample_slug(rec["url"])]
    known = existing_ws_bottle_url(item)
    if known:
        rec = next((r for _sc, r in hits if r["url"] == known), None)
        if rec is None:
            name, size, abv = slug_name(known)
            rec = {"url": known, "name": name, "size": size, "abv": abv, "_feat": es.features(name, abv)}
        hits = [(1.2, rec)] + [(sc, r) for sc, r in hits if r["url"] != known]
    bottles = [(sc, r) for sc, r in hits if BOTTLE_SLUG_RE.search(r["url"])]
    others = [(sc, r) for sc, r in hits if not BOTTLE_SLUG_RE.search(r["url"])]
    picked = []
    def add(row):
        if row[1]["url"] not in {r[1]["url"] for r in picked}:
            picked.append(row)
    if bottles:
        bottles.sort(key=lambda x: -x[0])
        add(bottles[0])
        if len(bottles) > 1 and bottles[1][0] >= 0.8:
            add(bottles[1])
    elif others:
        others.sort(key=lambda x: -x[0])
        add(others[0])
        if len(others) > 1 and others[1][0] >= 1.0:
            add(others[1])
    return picked


def needed(item: dict, descriptions: dict, notes: dict) -> bool:
    """True als er iets te vullen of te verbeteren valt (gat of stub, of lagere bron)."""
    iid = item["id"]
    entry = descriptions.get(iid) or {}
    text = (entry.get("text") or item.get("omschrijving") or "").strip()
    if not es.is_useful_description(text):
        return True
    url = entry.get("sourceUrl") or item.get("omschrijvingBron") or ""
    key = src.source_key(url)
    if key and key not in ("whiskysite", "whiskysitefles") and src.rank(SOURCE, "omschrijving") < src.rank(key, "omschrijving"):
        return True
    if not (item.get("proefnotities") or item.get("notities") or (notes.get(iid) or {}).get("text")):
        return True
    if not str(entry.get("image") or item.get("afbeelding") or "").startswith("https://"):
        return True
    return False


def should_replace_desc(item: dict, entry: dict | None) -> bool:
    have = ((entry or {}).get("text") or item.get("omschrijving") or "").strip()
    if not es.is_useful_description(have):
        return True
    url = (entry or {}).get("sourceUrl") or item.get("omschrijvingBron") or ""
    key = src.source_key(url)
    if not key or key in ("whiskysite", "whiskysitefles"):
        return False
    return src.rank(SOURCE, "omschrijving") < src.rank(key, "omschrijving")


def plan(repo: Path, s: requests.Session | None = None) -> dict:
    """url -> [(score, item_id)] voor whisky's die de flespagina nog kunnen gebruiken."""
    items = es.load_items(repo)
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    cat = load_catalog(s)
    idx = ex.build_index(cat)
    # Ook URLs die niet in de catalogusfilter zaten, maar wel als flesLink bekend zijn.
    by_url = defaultdict(list)
    records = {r["url"]: r for r in cat}
    for it in items:
        if not it.get("naam") or not needed(it, descriptions, notes):
            continue
        for sc, rec in candidates_for(it, idx):
            by_url[rec["url"]].append((round(sc, 3), it["id"]))
            records[rec["url"]] = rec
    return {"urls": dict(by_url), "records": records, "catalogus": len(cat)}


def html_cache_path(url: str) -> Path | None:
    path = fd.cache_path(WS_HTML, url)
    if path.is_file() and path.stat().st_size > 500:
        return path
    return None


def get_info(url: str, s: requests.Session) -> dict:
    path = cache_file(url)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    html_path = html_cache_path(url)
    if html_path:
        info = parse_html_page(html_path.read_text(encoding="utf-8", errors="replace"), url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return info
    resp = ff.http_get(s, url + "?format=json")
    time.sleep(DELAY)
    if resp.status_code != 200:
        return {"url": url, "bron": SOURCE, "fout": "http %s" % resp.status_code}
    try:
        data = resp.json()
    except ValueError:
        return {"url": url, "bron": SOURCE, "fout": "geen json"}
    if not data.get("product"):
        return {"url": url, "bron": SOURCE, "fout": "geen product"}
    info = parse_json_page(data, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info


def fetch_pages(repo: Path, max_pages: int = 0) -> Counter:
    s = session()
    p = plan(repo, s)
    urls = sorted(p["urls"], key=lambda u: -max(sc for sc, _ in p["urls"][u]))
    if max_pages:
        urls = urls[:max_pages]
    stats = Counter(catalogus=p["catalogus"], kandidaten=len(urls))
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "plan.json").write_text(
        json.dumps({"catalogus": p["catalogus"], "urls": p["urls"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    for n, url in enumerate(urls, 1):
        if cache_file(url).is_file():
            stats["cache"] += 1
            continue
        if html_cache_path(url):
            get_info(url, s)
            stats["htmlcache"] += 1
            continue
        try:
            info = get_info(url, s)
        except requests.RequestException as exc:
            stats["fout"] += 1
            print("fout %s %s" % (url, exc), file=sys.stderr)
            continue
        if info.get("fout"):
            stats["fout"] += 1
        else:
            stats["opgehaald"] += 1
        if n % 25 == 0:
            print("whiskysite fles: %s/%s" % (n, len(urls)), file=sys.stderr)
    return stats


def matches(repo: Path) -> tuple[dict, list[dict], Counter]:
    """Geaccepteerde flespagina's per whisky-id, plus zachte bijna-matches."""
    items = {it["id"]: it for it in es.load_items(repo)}
    plan_path = CACHE / "plan.json"
    if plan_path.is_file():
        planned = json.loads(plan_path.read_text(encoding="utf-8")).get("urls") or {}
    else:
        planned = plan(repo)["urls"]
    # Ook als plan ontbreekt: herbouw via catalogus.
    cat = load_catalog()
    idx = ex.build_index(cat)
    accepted: dict[str, tuple] = {}
    review: list[dict] = []
    stats = Counter()
    prev = ex.load_review(repo)
    # Itereer over items, niet alleen het plan: flesLink telt altijd mee.
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    seen_urls_for = defaultdict(list)
    for it in items.values():
        if not needed(it, descriptions, notes) and not existing_ws_bottle_url(it):
            # Toch meenemen als het plan deze whisky noemt (na een eerdere run).
            if not any(it["id"] in [i for _s, i in v] for v in planned.values()):
                continue
        hits = candidates_for(it, idx)
        best = None
        for sc, rec in hits:
            path = cache_file(rec["url"])
            if not path.is_file():
                stats["niet_opgehaald"] += 1
                continue
            info = json.loads(path.read_text(encoding="utf-8"))
            ok, why = info_ok(info)
            diffs = [why] if not ok else ex.differences(it, info, SOURCE)
            prev_row = prev.get((it["id"], rec["url"])) or {}
            if not diffs:
                if best is None or sc > best[0]:
                    best = (sc, info, "strikt")
            elif prev_row.get("goedgekeurd") is True:
                if best is None:
                    best = (sc, info, "goedgekeurd")
                stats["goedgekeurd"] += 1
            elif ex.near_match(diffs):
                review.append({
                    "id": it["id"],
                    "whisky": it["naam"],
                    "bron": "Whiskysite.nl (fles)",
                    "kandidaatNaam": info.get("titel") or rec["name"],
                    "kandidaatUrl": rec["url"],
                    "verschil": diffs,
                    "waarschijnlijkheid": ex.likelihood(sc, diffs),
                    "goedgekeurd": prev_row.get("goedgekeurd"),
                })
        if best:
            accepted[it["id"]] = best
            stats["match_" + best[2]] += 1
    return accepted, review, stats


def apply(repo: Path, descriptions: dict, notes: dict, facts: dict) -> dict:
    accepted, review, stats = matches(repo)
    by_id = {it["id"]: it for it in es.load_items(repo)}
    report = {"added_desc": [], "replaced_desc": [], "added_notes": [], "added_images": [], "added_facts": [], "matches": []}
    for iid, (sc, info, how) in accepted.items():
        item = by_id[iid]
        url = info["url"]
        report["matches"].append({
            "id": iid, "naam": item["naam"], "url": url,
            "titel": info.get("titel"), "hoe": how, "score": sc,
        })
        if not src.may_fill(SOURCE, "omschrijving"):
            continue
        text = info.get("omschrijving") or ""
        entry = descriptions.get(iid)
        if text and should_replace_desc(item, entry):
            # maybe_add_description weigert als de huidige tekst al bruikbaar is;
            # bij een lagere bron zetten we de huidige tekst tijdelijk weg (stub blijft bewaard).
            prev = dict(entry or {})
            useful_lower = es.is_useful_description((prev.get("text") or "").strip())
            if useful_lower:
                held = prev.get("text")
                prev["text"] = ""
                descriptions[iid] = prev
            if es.maybe_add_description(descriptions, iid, text, url, stats, item.get("naam")):
                stats["omschrijving"] += 1
                rec = {"id": iid, "naam": item["naam"], "url": url}
                if useful_lower:
                    report["replaced_desc"].append(rec)
                    stats["vervangen_lagere_bron"] += 1
                else:
                    report["added_desc"].append(rec)
            elif useful_lower:
                prev["text"] = held
                descriptions[iid] = prev
        if src.may_fill(SOURCE, "proefnotities"):
            note = ex.page_notes(info)
            if note and es.maybe_add_notes(notes, item, note, url, stats):
                stats["proefnotities"] += 1
                report["added_notes"].append({"id": iid, "naam": item["naam"], "url": url})
        img = info.get("afbeelding")
        entry = descriptions.get(iid) or {}
        has_img = str(entry.get("image") or item.get("afbeelding") or "").startswith("https://")
        if src.may_fill(SOURCE, "afbeelding") and img and str(img).startswith("https://") and not has_img:
            e = dict(entry)
            e.update({"image": img, "imageSource": SOURCE, "imagePage": url})
            descriptions[iid] = e
            stats["afbeelding"] += 1
            report["added_images"].append({"id": iid, "naam": item["naam"], "image": img, "url": url})
        vat_empty = not item.get("vattype") or str(item["vattype"]).lower().startswith("onbekend")
        fact = dict(facts.get(iid) or {})
        # Geen vattype-specificatie op de meeste Whiskysite-pagina's; attributes pikt het uit de tekst.
        if src.may_fill(SOURCE, "feiten") and vat_empty and (info.get("specs") or {}).get("vattype") and not fact.get("vattype"):
            fact["vattype"] = info["specs"]["vattype"].strip()
            fact["sourceUrl"] = fact.get("sourceUrl") or url
            facts[iid] = fact
            stats["feit_vattype"] += 1
    review.sort(key=lambda r: -r["waarschijnlijkheid"])
    report["review"] = review
    report["stats"] = dict(stats)
    return report


def page_texts(repo: Path) -> dict[str, list[tuple[str, str]]]:
    accepted, _review, _stats = matches(repo)
    out: dict[str, list[tuple[str, str]]] = {}
    for iid, (_sc, info, _how) in accepted.items():
        text = "\n".join(x for x in (info.get("omschrijving"), info.get("notities"), ex.spec_text(info)) if x)
        if text:
            out.setdefault(iid, []).append((info["url"], text))
    return out


def findable_report(repo: Path) -> dict:
    """Telling: hoeveel whisky's met een gat hebben (of krijgen) een Whiskysite-flespagina."""
    items = es.load_items(repo)
    descriptions = es.load_descriptions(repo / "descriptions.json")

    def hasd(i):
        return bool(i.get("omschrijving"))

    def hasn(i):
        return bool(i.get("proefnotities") or i.get("notities"))

    def hasi(i):
        return str(i.get("afbeelding") or "").startswith("http")

    gaps = [i for i in items if not (hasd(i) and hasn(i) and hasi(i))]
    cat = load_catalog()
    idx = ex.build_index(cat)
    already = 0
    strict = 0
    near = 0
    none = 0
    accepted, _rev, _st = matches(repo)
    rows = []
    for it in gaps:
        known = existing_ws_bottle_url(it)
        if known:
            already += 1
        hits = candidates_for(it, idx)
        kind = "geen"
        best_url = known
        title = ""
        diffs = []
        if it["id"] in accepted:
            kind = "strikt"
            _sc, info, _how = accepted[it["id"]]
            best_url = info["url"]
            title = info.get("titel") or ""
        else:
            for sc, rec in hits:
                path = cache_file(rec["url"])
                if path.is_file():
                    info = json.loads(path.read_text(encoding="utf-8"))
                    ok, why = info_ok(info)
                    d = [why] if not ok else ex.differences(it, info, SOURCE)
                else:
                    info = {"url": rec["url"], "titel": rec["name"], "specs": {}}
                    if rec.get("abv"):
                        info["specs"]["abv"] = str(rec["abv"]) + "%"
                    if rec.get("size"):
                        info["specs"]["inhoud"] = rec["size"]
                    d = [x for x in ex.differences(it, info, SOURCE) if not x.startswith("ABV onbekend")]
                    if is_sample_slug(rec["url"]):
                        d.append("geen reguliere fles")
                if not d:
                    kind = "strikt"
                    best_url = rec["url"]
                    title = rec["name"]
                    break
                if ex.near_match(d) and kind != "strikt":
                    kind = "bijna"
                    best_url = rec["url"]
                    title = rec.get("name") or ""
                    diffs = d
        if kind == "strikt":
            strict += 1
        elif kind == "bijna":
            near += 1
        else:
            none += 1
        rows.append({"id": it["id"], "naam": it["naam"], "soort": kind, "url": best_url, "titel": title, "verschil": diffs, "alInData": bool(known)})
    return {
        "gaten": len(gaps),
        "alInData_flesLink": already,
        "strikte_match": strict,
        "bijna_match": near,
        "geen": none,
        "whiskys": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report-findable", action="store_true")
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args(argv)
    if args.fetch:
        print("Ophalen:", dict(fetch_pages(args.repo, args.max_pages)), file=sys.stderr)
    if args.apply:
        repo = args.repo
        descriptions = es.load_descriptions(repo / "descriptions.json")
        notes = es.load_descriptions(repo / "notes.json")
        facts = es.load_descriptions(repo / "facts.json")
        report = apply(repo, descriptions, notes, facts)
        es.save_json(repo / "descriptions.json", descriptions)
        es.save_json(repo / "notes.json", notes)
        es.save_json(repo / "facts.json", facts)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        es.save_json(REPORT, report)
        print(report["stats"], file=sys.stderr)
    if args.report_findable:
        out = Path("/workspace/whisky_build/gaten/wsfles-vindbaar.json")
        data = findable_report(args.repo)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({k: v for k, v in data.items() if k != "whiskys"}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        # volledige rijen ernaast
        (CACHE / "vindbaar.json").parent.mkdir(parents=True, exist_ok=True)
        es.save_json(CACHE / "vindbaar.json", data)
        print("gaten %(gaten)s  al in data %(alInData_flesLink)s  strikt %(strikte_match)s  bijna %(bijna_match)s  geen %(geen)s" % data, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
