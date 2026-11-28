#!/usr/bin/env python3
"""Haal flesprijzen op bij Drankgigant.nl en DrankDozijn en schrijf prijzen.json.

Per whisky wordt alleen een prijs opgeslagen bij een zekere match: dezelfde
naam, leeftijd, editie/jaartal en vat, dezelfde ABV (±0,3) en dezelfde
flesmaat (standaard 70 cl), en alleen als de fles leverbaar is. Samples,
miniaturen en cadeausets tellen niet mee. Er wordt niets geraden: bij twijfel
wordt een pagina overgeslagen.

Bronnen
- Drankgigant (Magento): sitemap + gewone HTML met JSON-LD (offers.price).
- DrankDozijn (Nuxt, Cloudflare): sitemap + JSON-LD (offers.price). Kale HTTP
  wordt geblokkeerd; de pagina's worden daarom opgehaald met fetch() binnen
  een gewone Chrome-sessie die via DevTools (--cdp-port) bereikbaar is.
  DrankDozijn toont soms twee bedragen: een doorgestreepte oude prijs en de
  actuele prijs. De JSON-LD offers.price is de actuele verkoopprijs; de
  doorgestreepte prijs (StrikethroughPrice) wordt genegeerd. De validThrough van
  de prijs (meestal het einde van de dag) wordt als 'tot' bewaard; de prijs wordt
  bij elke run opnieuw opgehaald en is gedateerd met 'gecontroleerd'.

Uitvoer: prijzen.json naast de repo-root, {"items": {id: [prijsregel, ...]}}.
build_data.py voegt die regels samen met de bestaande flesprijs (laagste wint).
Cache van pagina-uitkomsten: /workspace/whisky_build/prijzen/cache.

Gebruik
  python scripts/fetch_flesprijzen.py --shop drankgigant
  python scripts/fetch_flesprijzen.py --shop drankdozijn --cdp-port 9333
  python scripts/fetch_flesprijzen.py --shop all --max-age-hours 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import enrich_sources as es

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path("/workspace/whisky_build/prijzen/cache")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
DG_SITEMAP = "https://www.drankgigant.nl/feeds/sitemapdgnl.xml"
DD_SITEMAP_INDEX = "https://drankdozijn.nl/sitemap_index.xml"
SHOP_NAMES = {"drankgigant": "Drankgigant.nl", "drankdozijn": "DrankDozijn"}
ABV_TOLERANCE = 0.3
DEFAULT_SIZE = "70 cl"

# Woorden in een winkeltitel die een fles geen losse reguliere fles maken.
BAD_TITLE_RE = re.compile(
    r"(?i)\b(sample|miniatuur|miniature|mini|giftset|gift\s*set|geschenk|cadeau|"
    r"tasting\s*(?:set|collection)|proefset|glas|glazen|set\s+van|duo|trio|"
    r"magnum|jeroboam|\d+\s*x\s*\d+|3\s*cl|5\s*cl|6\s*cl|10\s*cl|20\s*cl)\b"
)
GENERIC = es.STOPWORDS | {
    "cl", "ltr", "liter", "l", "ml", "scotland", "whisk", "proof",
    "finish", "finished", "matured", "maturation", "aged", "expression",
}
# Woorden die een andere uitgave aanduiden: ze moeten aan beide kanten voorkomen of aan geen.
DISTINCT_WORDS = {
    "limited", "special", "anniversary", "exclusive", "festival", "travel", "retail",
    "peated", "unpeated", "sherry", "bourbon", "port", "wine", "rum", "madeira",
    "private", "bottling", "rare",
}
SIZE_RE = re.compile(r"(?i)\b(\d+(?:[.,]\d+)?)\s*(cl|ml|ltr|l)\b")


def today() -> str:
    return datetime.now().strftime("%d-%m-%Y")


def size_label(value: str | None) -> str | None:
    """Normaliseer '70CL', '0,7 l', '700 ml' naar '70 cl'."""
    if not value:
        return None
    m = SIZE_RE.search(str(value))
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    cl = num * {"cl": 1, "ml": 0.1, "l": 100, "ltr": 100}[unit]
    cl = round(cl, 1)
    return "%s cl" % (int(cl) if cl == int(cl) else cl)


def parse_float(text) -> float | None:
    if text is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------

def http_get(session: requests.Session, url: str, timeout: int = 60, tries: int = 5):
    """GET met herhaling; Drankgigant breekt nu en dan een TLS-verbinding af."""
    last = None
    for attempt in range(tries):
        try:
            return session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def load_drankgigant_catalog(session: requests.Session) -> list[dict]:
    resp = http_get(session, DG_SITEMAP, timeout=120)
    resp.raise_for_status()
    out = []
    for block in re.findall(r"<url>(.*?)</url>", resp.text, re.S):
        loc = re.search(r"<loc>([^<]+)</loc>", block)
        if not loc:
            continue
        url = loc.group(1).strip()
        path = urlparse(url).path
        if not path.endswith(".html") or path.count("/") != 1:
            continue
        title = re.search(r"<image:title>([^<]*)</image:title>", block)
        name = (title.group(1) if title else path.strip("/")[:-5].replace("-", " "))
        name = re.sub(r"&amp;", "&", name).strip()
        out.append({"url": url, "name": name})
    return out


class Browser:
    """Minimale DevTools-client: fetch() binnen een tab die Cloudflare al heeft gehaald."""

    def __init__(self, port: int):
        import urllib.request
        import websocket  # pip install websocket-client

        tabs = json.load(urllib.request.urlopen("http://localhost:%d/json" % port))
        tab = [t for t in tabs if t["type"] == "page"][0]
        self.ws = websocket.create_connection(
            tab["webSocketDebuggerUrl"], timeout=90, suppress_origin=True
        )
        self.n = 0
        self.call("Page.enable")
        self.call("Page.navigate", url="https://drankdozijn.nl/robots.txt")
        for _ in range(30):
            time.sleep(1)
            title = self.eval("document.title") or ""
            if "moment" not in title.lower() and "controleren" not in title.lower():
                break

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                return msg

    def eval(self, expr):
        msg = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return msg.get("result", {}).get("result", {}).get("value")

    def fetch_text(self, url: str) -> tuple[int, str]:
        path = urlparse(url).path
        for attempt in range(3):
            res = self.eval(
                "fetch(%s).then(async r=>({s:r.status,t:await r.text()}))" % json.dumps(path)
            )
            if res and res["s"] == 200:
                return 200, res["t"]
            if res and res["s"] != 403:
                return res["s"], ""
            # Cloudflare-controle opnieuw laten passeren door de tab te verversen.
            time.sleep(3 * (attempt + 1))
            self.call("Page.navigate", url="https://drankdozijn.nl/robots.txt")
            time.sleep(4)
        return 403, ""


def load_drankdozijn_catalog(browser: Browser) -> list[dict]:
    status, index = browser.fetch_text(DD_SITEMAP_INDEX)
    if status != 200:
        raise SystemExit("DrankDozijn sitemap niet bereikbaar (%s)" % status)
    out = []
    for loc in re.findall(r"<loc>([^<]*products-\d+\.xml)</loc>", index):
        status, xml = browser.fetch_text(loc)
        if status != 200:
            continue
        for url in re.findall(r"<loc>([^<]+)</loc>", xml):
            if "/artikel/fles-" not in url or es.is_gift_or_box(url):
                continue
            out.append({"url": url.strip(), "name": es.slug_name_from_url(url)})
    return out


# --------------------------------------------------------------------------
# Matching op naam (voor het ophalen van pagina's; daarna volgt controle op de pagina zelf)
# --------------------------------------------------------------------------

def core_tokens(name: str) -> set[str]:
    feat = es.features(name)
    return {t for t in feat["tokens"] if t not in GENERIC}


def names_equivalent(item_name: str, cand_name: str) -> bool:
    """Strenger dan confident_match: de onderscheidende woorden moeten gelijk zijn."""
    item_name, cand_name = es.VOLUME_RE.sub(" ", item_name), es.VOLUME_RE.sub(" ", cand_name)
    a, b = core_tokens(item_name), core_tokens(cand_name)
    if a != b:
        return False
    fa = set(es.fold(item_name).split())
    fb = set(es.fold(cand_name).split())
    return (fa & DISTINCT_WORDS) == (fb & DISTINCT_WORDS)


def build_index(catalog: list[dict]) -> dict:
    idx = defaultdict(list)
    for rec in catalog:
        if BAD_TITLE_RE.search(rec["name"]):
            continue
        feat = es.features(rec["name"])
        if not feat["tokens"]:
            continue
        rec["_feat"] = feat
        for tok in feat["tokens"][:3]:
            idx[tok].append(rec)
    return idx


def candidates_for(item: dict, idx: dict) -> list[dict]:
    name = strip_sample(item["naam"])
    feat = es.features(name, item.get("abv"))
    hits, seen = [], set()
    for tok in feat["tokens"][:3]:
        for rec in idx.get(tok, []):
            if rec["url"] in seen:
                continue
            seen.add(rec["url"])
            if not es.confident_match(name, rec["name"], item.get("abv"), rec["_feat"]["abv"]):
                continue
            if not same_bottler(item, rec["name"]):
                continue
            if not names_equivalent(name, rec["name"]):
                continue
            hits.append(rec)
    return hits


def strip_sample(name: str) -> str:
    from build_data import strip_sample_markers

    return strip_sample_markers(name)


def same_bottler(item: dict, candidate_name: str) -> bool:
    """Een onafhankelijke botteling moet de bottelaar in de titel hebben, en andersom."""
    from build_data import detect_ib

    cand_ib = detect_ib({"naam": candidate_name})
    item_ib = item.get("ib") or detect_ib({"naam": item.get("naam")})
    if item_ib:
        return cand_ib == item_ib
    return cand_ib is None


# --------------------------------------------------------------------------
# Productpagina's
# --------------------------------------------------------------------------

def ld_products(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"])
                t = node.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    out.append(node)
    return out


def attribute(text: str, label: str) -> str | None:
    m = re.search(re.escape(label) + r"\s*\|\s*([^|]+?)\s*(?:\||$)", text)
    return m.group(1).strip() if m else None


def parse_product_page(html: str, url: str, shop: str) -> dict:
    """Haal naam, prijs, voorraad, ABV, maat en einddatum uit één productpagina."""
    prods = ld_products(html)
    info = {"url": url, "winkel": SHOP_NAMES[shop], "fout": None}
    if not prods:
        info["fout"] = "geen JSON-LD product"
        return info
    prod = prods[0]
    offers = prod.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if len(offers) == 1 else None
    if not isinstance(offers, dict):
        info["fout"] = "geen enkele offer"
        return info
    info["naam"] = prod.get("name")
    price = offers.get("price")
    info["prijs"] = round(float(price), 2) if price not in (None, "") else None
    info["voorraad"] = str(offers.get("availability", "")).rsplit("/", 1)[-1]
    # Actieprijs met einddatum; de doorgestreepte prijs is de oude prijs en telt niet mee.
    specs = offers.get("priceSpecification") or []
    if isinstance(specs, dict):
        specs = [specs]
    for spec in specs:
        if "Strikethrough" in str(spec.get("priceType", "")):
            info["doorgestreept"] = spec.get("price")
        elif spec.get("validThrough"):
            info["tot"] = str(spec["validThrough"])[:10]
    text = BeautifulSoup(html, "lxml").get_text("|", strip=True)
    info["abv"] = parse_float(attribute(text, "Alcoholpercentage"))
    info["grootte"] = size_label(attribute(text, "Inhoud")) or size_label(info.get("naam"))
    info["gecontroleerd"] = today()
    return info


def cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def get_page_info(url: str, shop: str, fetcher, max_age_hours: float, delay: float, stats) -> dict | None:
    path = cache_path(url)
    if path.is_file() and max_age_hours > 0:
        age = time.time() - path.stat().st_mtime
        if age < max_age_hours * 3600:
            stats["cache"] += 1
            return json.loads(path.read_text(encoding="utf-8"))
    status, html = fetcher(url)
    time.sleep(delay)
    if status != 200 or not html:
        stats["fout_http"] += 1
        return None
    info = parse_product_page(html, url, shop)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    stats["opgehaald"] += 1
    return info


def accept(item: dict, info: dict) -> tuple[bool, str]:
    """Laatste controle op de pagina zelf. Geeft (ok, reden)."""
    if info.get("fout") or info.get("prijs") is None:
        return False, info.get("fout") or "geen prijs"
    if info.get("voorraad") != "InStock":
        return False, "niet leverbaar"
    want = item.get("flesGrootte") or DEFAULT_SIZE
    if info.get("grootte") != size_label(want):
        return False, "flesmaat %s" % info.get("grootte")
    if info.get("abv") is None or item.get("abv") in (None, ""):
        return False, "ABV onbekend"
    if abs(float(item["abv"]) - float(info["abv"])) > ABV_TOLERANCE:
        return False, "ABV verschilt"
    if BAD_TITLE_RE.search(info.get("naam") or ""):
        return False, "geen reguliere fles"
    name = strip_sample(item["naam"])
    if not es.confident_match(name, info.get("naam") or "", item.get("abv"), info["abv"]):
        return False, "naam wijkt af"
    if not names_equivalent(name, info.get("naam") or ""):
        return False, "naam wijkt af"
    if info["prijs"] < 10:
        return False, "prijs onwaarschijnlijk"
    return True, ""


def price_line(info: dict) -> dict:
    line = {
        "winkel": info["winkel"],
        "prijs": info["prijs"],
        "link": info["url"],
        "grootte": info["grootte"],
        "voorraad": "ja",
        "gecontroleerd": info["gecontroleerd"],
    }
    if info.get("tot"):
        line["tot"] = info["tot"]
    return line


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shop", choices=["drankgigant", "drankdozijn", "all"], default="all")
    ap.add_argument("--cdp-port", type=int, default=9333, help="DevTools-poort van Chrome voor DrankDozijn.")
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--max-age-hours", type=float, default=20.0, help="Hergebruik pagina-uitkomsten jonger dan dit.")
    ap.add_argument("--delay", type=float, default=0.4, help="Pauze tussen paginaverzoeken (s).")
    ap.add_argument("--max-pages", type=int, default=0, help="Test: maximaal zoveel pagina's per winkel.")
    ap.add_argument("--out", type=Path, default=None, help="Uitvoerbestand (standaard prijzen.json in de repo).")
    ap.add_argument("--report", type=Path, default=Path("/workspace/whisky_build/prijzen/rapport.json"))
    args = ap.parse_args(argv)

    items = [it for it in es.load_items(args.repo) if it.get("naam")]
    out_path = args.out or (args.repo / "prijzen.json")
    previous = {}
    if out_path.is_file():
        previous = json.loads(out_path.read_text(encoding="utf-8")).get("items", {})

    shops = ["drankgigant", "drankdozijn"] if args.shop == "all" else [args.shop]
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    results: dict[str, list[dict]] = defaultdict(list)
    # Prijsregels van een winkel die we nu niet draaien blijven staan.
    for item_id, lines in previous.items():
        for line in lines:
            shop_key = [k for k, v in SHOP_NAMES.items() if v == line["winkel"]]
            if shop_key and shop_key[0] not in shops:
                results[item_id].append(line)

    report = {"datum": today(), "winkels": {}}
    for shop in shops:
        stats = defaultdict(int)
        rejected = []
        cat_path = CACHE_DIR / ("catalogus-%s.json" % shop)
        cached_catalog = None
        if cat_path.is_file() and time.time() - cat_path.stat().st_mtime < args.max_age_hours * 3600:
            cached_catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        if shop == "drankgigant":
            catalog = cached_catalog or load_drankgigant_catalog(session)

            def fetcher(url):
                try:
                    r = http_get(session, url)
                    return r.status_code, r.text
                except requests.RequestException:
                    return 0, ""
        else:
            browser = Browser(args.cdp_port)
            catalog = cached_catalog or load_drankdozijn_catalog(browser)
            fetcher = browser.fetch_text
        if not cached_catalog:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cat_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        stats["catalogus"] = len(catalog)
        idx = build_index(catalog)

        by_url: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            hits = candidates_for(item, idx)
            urls = {h["url"] for h in hits}
            if len(urls) == 1:
                by_url[hits[0]["url"]].append(item)
            elif len(urls) > 1:
                # Meerdere winkelpagina's met dezelfde kernnaam: alleen als de titels identiek zijn.
                names = {es.fold(h["name"]) for h in hits}
                stats["dubbelzinnig"] += 1
                if len(names) == 1:
                    by_url[hits[0]["url"]].append(item)
        stats["kandidaat_paginas"] = len(by_url)
        urls = sorted(by_url)
        if args.max_pages:
            urls = urls[: args.max_pages]
        for n, url in enumerate(urls, 1):
            info = get_page_info(url, shop, fetcher, args.max_age_hours, args.delay, stats)
            if not info:
                continue
            for item in by_url[url]:
                ok, why = accept(item, info)
                if not ok:
                    stats["afgewezen"] += 1
                    rejected.append({"id": item["id"], "naam": item["naam"], "pagina": info.get("naam"), "url": url, "reden": why})
                    continue
                results[item["id"]].append(price_line(info))
                stats["prijzen"] += 1
            if n % 50 == 0:
                print("%s: %s/%s pagina's" % (shop, n, len(urls)), file=sys.stderr)
        report["winkels"][shop] = {"stats": dict(stats), "afgewezen": rejected}
        print("%s: %s" % (shop, dict(stats)), file=sys.stderr)

    if out_path.is_file():
        # Een gelijktijdige run van de andere winkel niet overschrijven.
        latest = json.loads(out_path.read_text(encoding="utf-8")).get("items", {})
        for item_id, lines in latest.items():
            for line in lines:
                shop_key = [k for k, v in SHOP_NAMES.items() if v == line["winkel"]]
                if shop_key and shop_key[0] not in shops and line not in results[item_id]:
                    results[item_id].append(line)
    payload = {"datum": today(), "items": {k: sorted(v, key=lambda l: (l["prijs"], l["winkel"])) for k, v in sorted(results.items())}}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("prijzen.json: %s whisky's met een nieuwe prijsregel" % len(payload["items"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
