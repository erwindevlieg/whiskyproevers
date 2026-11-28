#!/usr/bin/env python3
"""Extra informatiebronnen voor de cascade: NL-winkels en bottelaarsites (geen prijzen).

Bronnen (zie `sources.py` voor de volgorde)
  dramshop, whiskybaseshop, drankenhandelleiden
      Lightspeed-winkels. De catalogus komt uit de openbare collectielijst
      (`/collection/?format=json`), de productgegevens uit `<product>.html?format=json`.
      robots.txt staat dit toe (crawl-delay 2 s wordt aangehouden).
      shop.whiskybase.com is de winkel, niet de Whiskybase-database.
  douglaslaing, northstar
      Shopify-sites van bottelaars (`/products.json`).
  cadenhead
      WooCommerce-site van de bottelaar (productsitemap + JSON-LD).

Matching (strikt, zelfde regels als Drankgigant)
  - zelfde bottelaar (`same_bottler`), `es.confident_match` op de naam aangevuld met de
    feiten van de pagina (ABV, vatnummer, leeftijd), ABV binnen 0,3 (bij een vatnummer 0,1),
    zelfde flesmaat; samples, miniaturen en cadeausets tellen niet;
  - daarnaast `names_equivalent` (onderscheidende woorden gelijk), of bij een single cask:
    hetzelfde vatnummer en hetzelfde distillatiejaar.
  Wat net niet door de check komt, gaat met de reden naar `data/match-review.json`.
  Alleen items die daar met "goedgekeurd": true staan, worden alsnog gebruikt.

Alles vult alleen LEGE velden (omschrijving, afbeelding, feiten); proefnotities worden
samengevoegd. Bestaande waarden worden nooit overschreven. Teksten zijn letterlijk.

Gebruik
  python scripts/enrich_extra.py --fetch            # catalogi + kandidaatpagina's ophalen (cache)
  python scripts/enrich_extra.py --apply            # cache verwerken + match-review.json schrijven
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import enrich_sources as es
import fetch_flesprijzen as ff
import sources as src

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path("/workspace/whisky_build/extra")
REPORT = CACHE / "apply-report.json"
REVIEW = ROOT / "data" / "match-review.json"

LIGHTSPEED = {
    "dramshop": {"base": "https://www.dramshop.nl/nl/", "delay": 2.0},
    "whiskybaseshop": {"base": "https://shop.whiskybase.com/nl/", "delay": 2.0},
    "drankenhandelleiden": {"base": "https://www.drankenhandelleiden.nl/", "delay": 2.0},
}
SHOPIFY = {
    "douglaslaing": {"base": "https://www.douglaslaing.com/", "delay": 1.0, "bottler": "Douglas Laing"},
    "northstar": {"base": "https://northstarspirits.com/", "delay": 1.0, "bottler": "North Star Spirits"},
}
WOO = {
    "cadenhead": {"base": "https://www.cadenhead.scot/", "delay": 2.0, "bottler": "Cadenhead"},
}
HUBSPOT = {
    "gordonmacphail": {"base": "https://www.gordonandmacphail.com/", "delay": 2.0, "bottler": "Gordon & MacPhail"},
}
ALL_SOURCES = list(LIGHTSPEED) + list(SHOPIFY) + list(WOO) + list(HUBSPOT)
BOTTLER_SOURCES = set(SHOPIFY) | set(WOO) | set(HUBSPOT)


def cfg_of(source: str) -> dict:
    return LIGHTSPEED.get(source) or SHOPIFY.get(source) or WOO.get(source) or HUBSPOT[source]

CASKNR_RE = re.compile(
    r"(?i)\b(?:cask|vat)\s*(?:no\.?|nr\.?|number|nummer|#)?\s*:?\s*#?\s*([A-Z]{0,3}\d[\d/+&-]*[A-Z]?)\b(?!\s*[,.]?\d*\s*%)"
)
TEXT_ABV_RE = re.compile(r"(?<![\d,.])(\d{2}(?:[,.]\d{1,2})?)\s?%")
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = ff.USER_AGENT
    s.headers["Accept-Language"] = "nl-NL,nl;q=0.9,en;q=0.8"
    return s


def cache_file(source: str, url: str) -> Path:
    return CACHE / source / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def html_text(fragment: str) -> str:
    """HTML naar tekst met alinea's (woorden ongewijzigd)."""
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "lxml")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        tag.append("\n\n" if tag.name in ("p", "h1", "h2", "h3", "h4") else "\n")
    text = soup.get_text("")
    text = _html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# Catalogi
# --------------------------------------------------------------------------

def load_catalog(source: str, s: requests.Session, max_age_h: float = 72) -> list[dict]:
    path = CACHE / ("catalogus-%s.json" % source)
    if path.is_file() and time.time() - path.stat().st_mtime < max_age_h * 3600:
        return json.loads(path.read_text(encoding="utf-8"))
    if source in LIGHTSPEED:
        cat = lightspeed_catalog(source, s)
    elif source in SHOPIFY:
        cat = shopify_catalog(source, s)
    elif source in HUBSPOT:
        cat = gm_catalog(source, s)
    else:
        cat = woo_catalog(source, s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")
    return cat


def lightspeed_catalog(source: str, s: requests.Session) -> list[dict]:
    cfg = LIGHTSPEED[source]
    out, page, pages = [], 1, 1
    while page <= pages:
        url = cfg["base"] + "collection/" + ("page%d.html" % page if page > 1 else "") + "?format=json&limit=100"
        resp = ff.http_get(s, url)
        time.sleep(cfg["delay"])
        if resp.status_code != 200:
            print("%s: collectie %s -> %s" % (source, page, resp.status_code), file=sys.stderr)
            break
        col = resp.json()["collection"]
        pages = int(col.get("pages") or 1)
        prods = col.get("products") or {}
        for p in (prods.values() if isinstance(prods, dict) else prods):
            out.append({
                "url": cfg["base"] + str(p.get("url") or "").lstrip("/"),
                "name": _html.unescape(p.get("fulltitle") or p.get("title") or ""),
                "kort": _html.unescape(p.get("description") or ""),
            })
        page += 1
    return out


def shopify_catalog(source: str, s: requests.Session) -> list[dict]:
    cfg = SHOPIFY[source]
    out, page = [], 1
    while True:
        resp = ff.http_get(s, cfg["base"] + "products.json?limit=250&page=%d" % page)
        time.sleep(cfg["delay"])
        if resp.status_code != 200:
            break
        prods = resp.json().get("products") or []
        if not prods:
            break
        for p in prods:
            out.append({
                "url": cfg["base"] + "products/" + p["handle"],
                "name": p.get("title") or "",
                "shopify": p,
            })
        page += 1
    return out


def gm_catalog(source: str, s: requests.Session) -> list[dict]:
    """Gordon & MacPhail: productpagina's uit de sitemap; slug = reeks-distilleerderij-jaar-ABV."""
    resp = ff.http_get(s, HUBSPOT[source]["base"] + "sitemap.xml")
    out, seen = [], set()
    for u in re.findall(r"<loc>([^<]+)</loc>", resp.text):
        m = re.search(r"/our-whiskies(?:-2)?/([^/]+)$", u.strip())
        if not m or m.group(1) in seen:
            continue
        slug = m.group(1)
        seen.add(slug)
        words = slug.split("-")
        # ABV staat als laatste getal: 479 -> 47.9, 43 -> 43
        abv = None
        for w in reversed(words):
            if re.fullmatch(r"\d{2,3}", w) and not re.fullmatch(r"(19|20)\d\d", w):
                abv = float(w) / (10 if len(w) == 3 else 1)
                break
        name = " ".join(w for w in words if not re.fullmatch(r"\d{5,}", w))
        if abv:
            name += " %s%%" % abv
        out.append({"url": u.strip(), "name": name + " Gordon & MacPhail"})
    return out


def woo_catalog(source: str, s: requests.Session) -> list[dict]:
    cfg = WOO[source]
    resp = ff.http_get(s, cfg["base"] + "product-sitemap.xml")
    urls = re.findall(r"<loc>([^<]+)</loc>", resp.text) if resp.status_code == 200 else []
    out = []
    for u in urls:
        if "/product/" not in u and "/shop/" not in u:
            continue
        out.append({"url": u.strip(), "name": es.slug_name_from_url(u.strip())})
    return out


# --------------------------------------------------------------------------
# Kandidaten (losse voorselectie op naam; de strikte check volgt op de pagina)
# --------------------------------------------------------------------------

def bottler_of(name: str) -> str | None:
    from build_data import detect_ib

    return detect_ib({"naam": name})


def item_bottler(item: dict) -> str | None:
    return item.get("ib") or bottler_of(item.get("naam") or "")


def build_index(catalog: list[dict]) -> dict:
    idx = defaultdict(list)
    for rec in catalog:
        if ff.BAD_TITLE_RE.search(rec["name"]) and not re.search(r"(?i)\b70\s*cl\b", rec["name"]):
            continue
        feat = es.features(rec["name"])
        if not feat["tokens"]:
            continue
        rec["_feat"] = feat
        for tok in set(es.brand_head(feat["tokens"])[:1] + feat["tokens"][:2]):
            idx[tok].append(rec)
    return idx


def loose_score(item: dict, name: str, rec: dict, source: str) -> float:
    """Hoe waarschijnlijk is rec dezelfde whisky? 0 = nee. Alleen voor voorselectie/review."""
    a = es.features(name, item.get("abv"))
    b = rec["_feat"]
    if not es.same_brand(name, rec["name"], a, b):
        return 0
    ib = item_bottler(item)
    cb = bottler_of(rec["name"])
    if source in BOTTLER_SOURCES:
        cb = cb or cfg_of(source)["bottler"]
    if ib != cb and not (ib is None and cb is None):
        return 0
    if a["age"] and b["age"] and a["age"] != b["age"]:
        return 0
    if a["vintages"] and b["vintages"] and a["vintages"].isdisjoint(b["vintages"]):
        return 0
    if a["casks"] and b["casks"] and a["casks"].isdisjoint(b["casks"]):
        return 0
    if a["abv"] and b["abv"] and abs(a["abv"] - b["abv"]) > 1.0:
        return 0
    overlap = len(a["token_set"] & b["token_set"])
    union = len(a["token_set"] | b["token_set"]) or 1
    score = overlap / union
    if a["age"] and a["age"] == b["age"]:
        score += 0.3
    if a["vintages"] and not a["vintages"].isdisjoint(b["vintages"]):
        score += 0.3
    if a["casks"] and not a["casks"].isdisjoint(b["casks"]):
        score += 0.5
    if ib and ib == cb:
        score += 0.1
    return score if overlap >= 2 else 0


def gm_candidates(item: dict, catalog: list[dict], top: int = 3) -> list[tuple[float, dict]]:
    """G&M-slugs beginnen met een code en reeks; match op distilleerderij + jaartal + ABV."""
    if item_bottler(item) != "Gordon & MacPhail":
        return []
    name = ff.strip_sample(item["naam"])
    feat = es.features(name, item.get("abv"))
    head = "-".join(es.brand_head(feat["tokens"]))
    if not head:
        return []
    out = []
    for rec in catalog:
        slug = rec["url"].rsplit("/", 1)[-1]
        if "-%s-" % head not in "-%s-" % slug:
            continue
        rf = es.features(rec["name"])
        if feat["vintages"] and (not rf["vintages"] or feat["vintages"].isdisjoint(rf["vintages"])):
            continue
        sc = 0.5
        if feat["abv"] and rf["abv"]:
            if abs(feat["abv"] - rf["abv"]) <= 0.3:
                sc += 0.5
            elif abs(feat["abv"] - rf["abv"]) > 1.5:
                continue
        out.append((sc, rec))
    out.sort(key=lambda x: -x[0])
    return out[:top]


def candidates(item: dict, idx: dict, source: str, top: int = 3) -> list[tuple[float, dict]]:
    if source in HUBSPOT:
        return gm_candidates(item, idx["_catalog"], top)
    name = ff.strip_sample(item["naam"])
    feat = es.features(name, item.get("abv"))
    seen, scored = set(), []
    for tok in set(es.brand_head(feat["tokens"])[:1] + feat["tokens"][:2]):
        for rec in idx.get(tok, []):
            if rec["url"] in seen:
                continue
            seen.add(rec["url"])
            sc = loose_score(item, name, rec, source)
            if sc >= 0.45:
                scored.append((sc, rec))
    scored.sort(key=lambda x: -x[0])
    return scored[:top]


# --------------------------------------------------------------------------
# Productpagina's
# --------------------------------------------------------------------------

SPEC_KEYS = {
    "abv": ("alcoholpercentage", "alcohol", "abv", "alcohol percentage", "alc"),
    "inhoud": ("inhoud", "volume", "size", "content", "flesinhoud"),
    "vattype": ("vat type", "vattype", "cask type", "vat", "cask", "rijping", "type vat"),
    "vatnummer": ("vatnummer", "cask number", "cask no", "vat nummer", "caskno", "cask nr"),
    "oplage": ("aantal flessen gebotteld", "aantal flessen", "bottles", "outturn", "oplage", "number of bottles"),
    "leeftijd": ("leeftijd", "age", "jaren", "leeftijd in jaren"),
    "distillatie": ("distillatiedatum", "gedistilleerd", "distilled", "distillation date", "destillatiedatum", "distillatiejaar", "vintage"),
    "botteling": ("bottelingsdatum", "gebotteld", "bottled", "bottling date", "bottelingsjaar"),
}


def norm_specs(raw: dict) -> dict:
    out = {}
    for key, val in raw.items():
        k = re.sub(r"\s+", " ", str(key).strip().lower()).rstrip(":")
        for field, names in SPEC_KEYS.items():
            if k in names and field not in out and str(val).strip():
                out[field] = str(val).strip()
    return out


def lightspeed_info(source: str, url: str, s: requests.Session) -> dict:
    info = {"url": url, "bron": source, "fout": None}
    resp = ff.http_get(s, url + "?format=json")
    if resp.status_code != 200:
        info["fout"] = "http %s" % resp.status_code
        return info
    try:
        data = resp.json()
    except ValueError:
        info["fout"] = "geen json"
        return info
    p = data.get("product") or {}
    shop_id = (data.get("shop") or {}).get("id")
    info["titel"] = _html.unescape(p.get("fulltitle") or p.get("title") or "")
    content = html_text(p.get("content") or "")
    short = _html.unescape(p.get("description") or "").strip()
    info["omschrijving"] = content if len(content) >= len(short) else short
    raw = {}
    for spec in (p.get("specs") or {}).values() if isinstance(p.get("specs"), dict) else (p.get("specs") or []):
        raw[spec.get("title") or ""] = _html.unescape(str(spec.get("value") or ""))
    info["specsRaw"] = raw
    info["specs"] = norm_specs(raw)
    img = p.get("image")
    if img and shop_id:
        info["afbeelding"] = "https://cdn.webshopapp.com/shops/%s/files/%s/image.jpg" % (shop_id, img)
    return info


def shopify_info(source: str, rec: dict) -> dict:
    p = rec.get("shopify") or {}
    info = {"url": rec["url"], "bron": source, "fout": None, "titel": p.get("title") or rec["name"]}
    body = html_text(p.get("body_html") or "")
    info["omschrijving"] = body
    raw = {}
    # Feiten staan bij deze bottelaars als regels "ABV: 48%" of in de tags.
    for ln in body.split("\n"):
        m = re.match(r"\s*([A-Za-z][A-Za-z .]{1,30})\s*[:\-–]\s*(.+)$", ln)
        if m:
            raw.setdefault(m.group(1).strip(), m.group(2).strip())
    info["specsRaw"] = raw
    info["specs"] = norm_specs(raw)
    m = es.ABV_RE.search(info["titel"].replace(",", ".")) or es.ABV_RE.search(body.replace(",", "."))
    if "abv" not in info["specs"] and m:
        info["specs"]["abv"] = m.group(0)
    m = re.search(r"(?i)(\d{2,4})\s*(ml|cl)\b", body)
    if "inhoud" not in info["specs"] and m:
        info["specs"]["inhoud"] = m.group(0)
    imgs = p.get("images") or []
    if imgs and imgs[0].get("src"):
        info["afbeelding"] = imgs[0]["src"].split("?")[0]
    return info


def woo_info(source: str, url: str, s: requests.Session) -> dict:
    info = {"url": url, "bron": source, "fout": None}
    resp = ff.http_get(s, url)
    if resp.status_code != 200:
        info["fout"] = "http %s" % resp.status_code
        return info
    prods = ff.ld_products(resp.text)
    soup = BeautifulSoup(resp.text, "lxml")
    h1 = soup.select_one("h1.product_title") or soup.select_one("h1")
    info["titel"] = (prods[0].get("name") if prods else None) or (h1.get_text(" ", strip=True) if h1 else "")
    node = soup.select_one("#tab-description") or soup.select_one(".woocommerce-product-details__short-description")
    info["omschrijving"] = html_text(str(node)) if node else (html_text(prods[0].get("description") or "") if prods else "")
    info["omschrijving"] = re.sub(r"^\s*(Description|Beschrijving)\s*\n+", "", info["omschrijving"])
    raw = {}
    for row in soup.select("table.woocommerce-product-attributes tr, table.shop_attributes tr"):
        th, td = row.find("th"), row.find("td")
        if th and td:
            raw[th.get_text(" ", strip=True)] = td.get_text(" ", strip=True)
    info["specsRaw"] = raw
    info["specs"] = norm_specs(raw)
    if prods:
        img = prods[0].get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        if isinstance(img, str) and img.startswith("http"):
            info["afbeelding"] = img
    return info


def gm_info(source: str, url: str, s: requests.Session) -> dict:
    info = {"url": url, "bron": source, "fout": None}
    resp = ff.http_get(s, url)
    if resp.status_code != 200:
        info["fout"] = "http %s" % resp.status_code
        return info
    soup = BeautifulSoup(resp.text, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    lines = soup.get_text("\n", strip=True).split("\n")
    raw = {}
    labels = {"Range", "Region", "Distillery", "Strength", "Vintage", "Age", "Bottled year", "Cask type", "Colour", "Market", "Cask number", "Cask Number", "Bottles", "Outturn"}
    try:
        start = lines.index("Range")
    except ValueError:
        start = None
    if start is not None:
        i = start
        while i + 1 < len(lines) and lines[i] in labels:
            raw[lines[i]] = lines[i + 1]
            i += 2
        notes = []
        if "Tasting Notes" in lines[i:i + 3]:
            j = lines.index("Tasting Notes", i)
            k = j + 1
            while k + 1 < len(lines) and lines[k] in ("Nose", "Taste", "Palate", "Finish"):
                notes.append("%s: %s" % (lines[k], lines[k + 1]))
                k += 2
        info["notities"] = "\n".join(notes)
    # Paginatitel is "23/012 CC Glentauchers 1990 47.9%"; voor de naamcheck bouwen we
    # "Glentauchers 1990 Connoisseurs Choice 47.9% Gordon & MacPhail" uit de velden van de pagina.
    info["paginatitel"] = title
    parts = [raw.get("Distillery") or "", raw.get("Vintage") or "", raw.get("Range") or "", raw.get("Strength") or "", "Gordon & MacPhail"]
    info["titel"] = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if not raw.get("Distillery"):
        info["fout"] = "geen productgegevens op pagina"
    info["omschrijving"] = ""
    raw2 = dict(raw)
    if raw.get("Strength"):
        raw2["abv"] = raw["Strength"]
    if raw.get("Age"):
        raw2["leeftijd"] = raw["Age"]
    if raw.get("Cask type"):
        raw2["vat type"] = raw["Cask type"]
    if raw.get("Bottled year"):
        raw2["bottled"] = raw["Bottled year"]
    if raw.get("Vintage"):
        raw2["vintage"] = raw["Vintage"]
    info["specsRaw"] = raw
    info["specs"] = norm_specs(raw2)
    img = soup.select_one('img[fetchpriority="high"]') or soup.select_one('img[src*="main_image"]')
    if img and img.get("src", "").startswith("https://"):
        info["afbeelding"] = img["src"]
    return info


HYPHEN_AGE_RE = re.compile(r"(?i)\b(\d{1,2})[\s-]*(?:-?year-?old|-?years?-?old|yo|y\.o\.?|y\.)(?=\W|$)")


def composed_name(info: dict) -> str:
    """Titel aangevuld met feiten van de pagina zelf (ABV, vatnummer, leeftijd), voor confident_match."""
    name = HYPHEN_AGE_RE.sub(lambda m: "%s Years Old" % m.group(1), info.get("titel") or "")
    sp = info.get("specs") or {}
    extra = []
    age = ff.parse_float(sp.get("leeftijd"))
    if age and not es.AGE_RE.search(name) and 3 <= age <= 80 and float(age).is_integer():
        extra.append("%d Years Old" % age)
    nr = sp.get("vatnummer")
    if nr and not CASKNR_RE.search(name):
        nr = re.sub(r"(?i)^(cask|vat)\s*(no\.?|nr\.?|#)?\s*", "", nr).strip()
        if nr:
            extra.append("Cask %s" % nr)
    return " ".join([name] + extra)


def page_abv(info: dict) -> float | None:
    """ABV uit de specificaties, anders uit titel of omschrijving, maar alleen als daar
    precies één alcoholpercentage in staat (Whiskybase Shop en Drankenhandel Leiden zetten
    het percentage in de tekst)."""
    val = (info.get("specs") or {}).get("abv")
    if val:
        return ff.parse_float(str(val).replace(",", "."))
    found = set()
    for text in (info.get("titel") or "", info.get("omschrijving") or ""):
        for m in TEXT_ABV_RE.finditer(text):
            v = float(m.group(1).replace(",", "."))
            if 35 <= v <= 75:
                found.add(v)
    return found.pop() if len(found) == 1 else None


def page_size(info: dict) -> str | None:
    raw = (info.get("specs") or {}).get("inhoud") or ""
    raw = re.sub(r"(?i)\b(liters?|litre)\b", "l", str(raw)).replace(",", ".")
    return ff.size_label(raw) or ff.size_label(info.get("titel"))


# --------------------------------------------------------------------------
# Strikte check + verschil voor review
# --------------------------------------------------------------------------

def casks_of(name: str) -> set[str]:
    return {re.sub(r"\W", "", m.group(1)).lower() for m in CASKNR_RE.finditer(name or "")}


def differences(item: dict, info: dict, source: str) -> list[str]:
    """Alle verschillen tussen whisky en pagina (leeg = strikte match)."""
    diffs = []
    if info.get("fout"):
        return [info["fout"]]
    name = ff.strip_sample(item["naam"])
    cand = composed_name(info)
    titel = info.get("titel") or ""
    if ff.BAD_TITLE_RE.search(titel) and not re.search(r"(?i)\b70\s*cl\b", titel):
        diffs.append("geen reguliere fles")
    want = ff.size_label(item.get("flesGrootte") or ff.DEFAULT_SIZE)
    size = page_size(info)
    if size and size != want and not (source in BOTTLER_SOURCES and size in ("70 cl", "75 cl", "50 cl")):
        diffs.append("maat %s vs %s" % (want, size))
    pa, ia = page_abv(info), es.parse_abv(item.get("abv"))
    if pa is None or ia is None:
        diffs.append("ABV onbekend op pagina" if pa is None else "ABV onbekend")
    elif abs(pa - ia) > ff.ABV_TOLERANCE:
        diffs.append("ABV %s vs %s" % (fmt(ia), fmt(pa)))
    ib = item_bottler(item)
    cb = bottler_of(titel) or (bottler_of(cand))
    if source in BOTTLER_SOURCES:
        cb = cb or cfg_of(source)["bottler"]
    if ib != cb:
        diffs.append("bottelaar %s vs %s" % (ib or "geen", cb or "geen"))
    a, b = es.features(name), es.features(cand)
    ac, bc = casks_of(name), casks_of(cand)
    # Single cask: zelfde vatnummer + zelfde distillatiejaar + ABV binnen 0,1 + zelfde bottelaar
    # wijst één vat aan; dan hoeft de leeftijd niet ook in de titel te staan.
    same_cask = bool(
        ac and bc and not ac.isdisjoint(bc) and a["vintages"] and not a["vintages"].isdisjoint(b["vintages"])
        and pa is not None and ia is not None and abs(pa - ia) <= 0.11 and ib == cb
    )
    if a["age"] and b["age"] and a["age"] != b["age"]:
        diffs.append("leeftijd %s vs %s" % (a["age"], b["age"]))
    elif a["age"] and not b["age"] and str(a["age"]) not in b["folded"].split() and not same_cask:
        diffs.append("leeftijd %s niet op pagina" % a["age"])
    elif b["age"] and not a["age"] and not same_cask:
        diffs.append("pagina noemt leeftijd %s" % b["age"])
    if ac and bc and ac.isdisjoint(bc):
        diffs.append("vatnummer %s vs %s" % ("/".join(sorted(ac)), "/".join(sorted(bc))))
    elif ac and not bc:
        diffs.append("vatnummer %s niet op pagina" % "/".join(sorted(ac)))
    if (a["batches"] or b["batches"]) and (not a["batches"] or not b["batches"] or a["batches"].isdisjoint(b["batches"])):
        diffs.append("batch %s vs %s" % ("/".join(sorted(a["batches"])) or "-", "/".join(sorted(b["batches"])) or "-"))
    years_b = b["vintages"] | b["release_years"]
    years_a = a["vintages"] | a["release_years"]
    if years_b and not years_a and not same_cask:
        diffs.append("pagina noemt jaartal %s" % "/".join(sorted(years_b)))
    if a["vintages"] and b["vintages"] and a["vintages"].isdisjoint(b["vintages"]):
        diffs.append("jaartal %s vs %s" % ("/".join(sorted(a["vintages"])), "/".join(sorted(b["vintages"]))))
    ea, eb = a["editions"] - {"sherry", "bourbon"}, b["editions"] - {"sherry", "bourbon"}
    if ea != eb:
        only_a = sorted(e for e in ea - eb if e not in b["folded"])
        only_b = sorted(e for e in eb - ea if e not in a["folded"])
        if only_a or only_b:
            diffs.append("editie %s vs %s" % (", ".join(only_a) or "-", ", ".join(only_b) or "-"))
    # Naamcheck altijd (ook als er al andere verschillen zijn), met de ABV van de whisky aan
    # beide kanten zodat een ABV-verschil hier niet dubbel telt.
    if True:
        ok_name = es.confident_match(name, cand, ia, ia) or (same_cask and es.same_brand(name, cand))
        equiv = ff.names_equivalent(name, cand)
        cask_ok = same_cask
        if not ok_name and not any(d.startswith(("leeftijd", "pagina noemt", "vatnummer", "editie", "batch", "jaartal")) for d in diffs):
            diffs.append("naam afwijkend (geen zekere naam-match)")
        elif ok_name and not equiv and not cask_ok:
            ca, cb2 = ff.core_tokens(es.VOLUME_RE.sub(" ", name)), ff.core_tokens(es.VOLUME_RE.sub(" ", cand))
            diffs.append("naam afwijkend (%s | %s)" % (" ".join(sorted(ca - cb2)) or "-", " ".join(sorted(cb2 - ca)) or "-"))
    return diffs


def fmt(x) -> str:
    return ("%g" % float(x)).replace(".", ",")


# --------------------------------------------------------------------------
# Ophalen
# --------------------------------------------------------------------------

def gap_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if not it.get("naam"):
            continue
        if not (it.get("omschrijving") and (it.get("proefnotities") or it.get("notities")) and str(it.get("afbeelding") or "").startswith("http")):
            out.append(it)
    return out


def plan(repo: Path, s: requests.Session, sources: list[str]) -> dict:
    """{bron: {url: [(score, item_id)]}} voor alle whisky's met een gat."""
    items = gap_items(es.load_items(repo))
    result = {}
    for source in sources:
        cat = load_catalog(source, s)
        idx = build_index(cat) if source not in HUBSPOT else {"_catalog": cat}
        by_url = defaultdict(list)
        for it in items:
            for sc, rec in candidates(it, idx, source):
                by_url[rec["url"]].append((round(sc, 3), it["id"]))
        result[source] = {"catalogus": len(cat), "urls": dict(by_url), "records": {r["url"]: r for r in cat if r["url"] in by_url}}
        print("%s: catalogus %s, kandidaatpagina's %s" % (source, len(cat), len(by_url)), file=sys.stderr)
    return result


def fetch(repo: Path, sources: list[str], max_pages: int = 0) -> Counter:
    s = session()
    stats = Counter()
    p = plan(repo, s, sources)
    for source in sources:
        urls = sorted(p[source]["urls"], key=lambda u: -max(sc for sc, _ in p[source]["urls"][u]))
        if max_pages:
            urls = urls[:max_pages]
        delay = cfg_of(source)["delay"]
        for n, url in enumerate(urls, 1):
            path = cache_file(source, url)
            if path.is_file():
                stats[source + ":cache"] += 1
                continue
            try:
                if source in LIGHTSPEED:
                    info = lightspeed_info(source, url, s)
                    time.sleep(delay)
                elif source in SHOPIFY:
                    info = shopify_info(source, p[source]["records"][url])
                elif source in HUBSPOT:
                    info = gm_info(source, url, s)
                    time.sleep(delay)
                else:
                    info = woo_info(source, url, s)
                    time.sleep(delay)
            except requests.RequestException as exc:
                stats[source + ":fout"] += 1
                print("fout %s %s" % (url, exc), file=sys.stderr)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            stats[source + ":opgehaald"] += 1
            if n % 50 == 0:
                print("%s: %s/%s" % (source, n, len(urls)), file=sys.stderr)
    (CACHE / "plan.json").write_text(json.dumps({k: {"catalogus": v["catalogus"], "urls": v["urls"]} for k, v in p.items()}, ensure_ascii=False), encoding="utf-8")
    return stats


# --------------------------------------------------------------------------
# Verwerken
# --------------------------------------------------------------------------

HARD = re.compile(r"^(bottelaar|jaartal|batch|vatnummer \S+ vs|leeftijd \d+ vs|geen reguliere fles|http|geen json|geen productgegevens)")


def near_match(diffs: list[str]) -> bool:
    """Net niet: alleen 'zachte' verschillen (ABV klein, maat, ontbrekende leeftijd, naam/editie)."""
    if not diffs or len(diffs) > 2:
        return False
    if len(diffs) > 1 and any(d.startswith("naam") for d in diffs):
        return False
    for d in diffs:
        if HARD.match(d):
            return False
        m = re.match(r"ABV (\S+) vs (\S+)", d)
        if m and abs(float(m.group(1).replace(",", ".")) - float(m.group(2).replace(",", "."))) > 1.0:
            return False
    return True


def likelihood(score: float, diffs: list[str]) -> float:
    pen = 0.0
    for d in diffs:
        if d.startswith("ABV") and "onbekend" not in d:
            m = re.match(r"ABV (\S+) vs (\S+)", d)
            pen += 0.2 + abs(float(m.group(1).replace(",", ".")) - float(m.group(2).replace(",", ".")))
        elif d.startswith("maat"):
            pen += 0.15
        elif "niet op pagina" in d or "noemt leeftijd" in d or "noemt jaartal" in d:
            pen += 0.25
        elif "onbekend" in d:
            pen += 0.35
        else:
            pen += 0.45
    return round(score - pen, 3)


def load_review(repo: Path) -> dict:
    path = repo / "data" / "match-review.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(r["id"], r["kandidaatUrl"]): r for r in data.get("kandidaten", [])}


def spec_text(info: dict) -> str:
    """Feiten van de pagina als korte regels, zodat attributes.py ze herkent."""
    sp = info.get("specs") or {}
    lines = []
    if sp.get("vattype"):
        lines.append("Vat type: %s" % sp["vattype"])
    if sp.get("vatnummer"):
        lines.append("Cask number: %s" % re.sub(r"(?i)^(cask|vat)\s*(no\.?|nr\.?|#)?\s*", "", sp["vatnummer"]))
    if sp.get("oplage") and re.fullmatch(r"\d[\d.,]*", sp["oplage"].strip()):
        lines.append("Oplage: %s flessen" % sp["oplage"].strip())
    if sp.get("distillatie") and YEAR_RE.search(sp["distillatie"]):
        lines.append("Distilled: %s" % YEAR_RE.search(sp["distillatie"]).group(1))
    if sp.get("botteling"):
        m = YEAR_RE.search(sp["botteling"]) or re.search(r"-(\d{2})$", sp["botteling"])
        if m:
            y = m.group(1)
            lines.append("Bottled: %s" % (y if len(y) == 4 else "20" + y))
    return "\n".join(lines)


def page_notes(info: dict) -> str:
    if info.get("notities"):
        return info["notities"]
    import enrich_drankgigant as edg

    return edg.notes_from_info(info.get("omschrijving") or "", {})


def matches(repo: Path, sources: list[str] | None = None) -> tuple[dict, list[dict], Counter]:
    """Geeft ({item_id: [(bron, info)]} geaccepteerd, review-kandidaten, stats)."""
    sources = sources or ALL_SOURCES
    items = es.load_items(repo)
    by_id = {it["id"]: it for it in items}
    review_prev = load_review(repo)
    plan_path = CACHE / "plan.json"
    accepted: dict[str, list] = defaultdict(list)
    review: list[dict] = []
    stats = Counter()
    for source in sources:
        cat_path = CACHE / ("catalogus-%s.json" % source)
        if not cat_path.is_file():
            continue
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        idx = build_index(cat) if source not in HUBSPOT else {"_catalog": cat}
        for it in gap_items(items):
            best = None
            for sc, rec in candidates(it, idx, source):
                path = cache_file(source, rec["url"])
                if not path.is_file():
                    stats[source + ":niet_opgehaald"] += 1
                    continue
                info = json.loads(path.read_text(encoding="utf-8"))
                diffs = differences(it, info, source)
                prev = review_prev.get((it["id"], rec["url"])) or {}
                if not diffs:
                    if best is None or sc > best[0]:
                        best = (sc, info, "strikt")
                elif prev.get("goedgekeurd") is True:
                    if best is None:
                        best = (sc, info, "goedgekeurd")
                    stats[source + ":goedgekeurd"] += 1
                elif near_match(diffs):
                    review.append({
                        "id": it["id"],
                        "whisky": it["naam"],
                        "bron": src.SOURCES.get(source, {}).get("label", source),
                        "kandidaatNaam": info.get("paginatitel") or info.get("titel"),
                        "kandidaatUrl": rec["url"],
                        "verschil": diffs,
                        "waarschijnlijkheid": likelihood(sc, diffs),
                        "goedgekeurd": prev.get("goedgekeurd"),
                    })
            if best:
                accepted[it["id"]].append((source, best[1], best[2]))
                stats[source + ":match_" + best[2]] += 1
    return accepted, review, stats


def apply(repo: Path, descriptions: dict, notes: dict, facts: dict, sources: list[str] | None = None) -> dict:
    """Vul lege velden uit geaccepteerde matches, in cascadevolgorde. Muteert de dicts."""
    accepted, review, stats = matches(repo, sources)
    by_id = {it["id"]: it for it in es.load_items(repo)}
    report = {"added_desc": [], "added_notes": [], "added_images": [], "added_facts": [], "matches": []}
    for iid, hits in accepted.items():
        item = by_id[iid]
        hits.sort(key=lambda h: src.rank(h[0], "omschrijving"))
        for source, info, how in hits:
            url = info["url"]
            report["matches"].append({"id": iid, "naam": item["naam"], "bron": source, "url": url, "titel": info.get("paginatitel") or info.get("titel"), "hoe": how})
            text = info.get("omschrijving") or ""
            if src.may_fill(source, "omschrijving") and text:
                have = (item.get("omschrijving") or (descriptions.get(iid) or {}).get("text") or "").strip()
                if not es.is_useful_description(have):
                    if es.maybe_add_description(descriptions, iid, text, url, stats, item.get("naam")):
                        stats[source + ":omschrijving"] += 1
                        report["added_desc"].append({"id": iid, "naam": item["naam"], "url": url})
            note = page_notes(info)
            if src.may_fill(source, "proefnotities") and note and es.maybe_add_notes(notes, item, note, url, stats):
                stats[source + ":proefnotities"] += 1
                report["added_notes"].append({"id": iid, "naam": item["naam"], "url": url})
            img = info.get("afbeelding")
            entry = descriptions.get(iid) or {}
            has_img = str(entry.get("image") or item.get("afbeelding") or "").startswith("https://")
            if src.may_fill(source, "afbeelding") and img and img.startswith("https://") and not has_img:
                e = dict(entry)
                e.update({"image": img, "imageSource": source, "imagePage": url})
                descriptions[iid] = e
                stats[source + ":afbeelding"] += 1
                report["added_images"].append({"id": iid, "naam": item["naam"], "image": img, "url": url})
            vat = (info.get("specs") or {}).get("vattype")
            fact = dict(facts.get(iid) or {})
            vat_empty = not item.get("vattype") or str(item["vattype"]).lower().startswith("onbekend")
            if src.may_fill(source, "feiten") and vat and vat_empty and not fact.get("vattype"):
                fact["vattype"] = vat.strip()
                fact["sourceUrl"] = fact.get("sourceUrl") or url
                facts[iid] = fact
                stats[source + ":feit_vattype"] += 1
                report["added_facts"].append({"id": iid, "naam": item["naam"], "velden": {"vattype": vat}, "url": url})
    review.sort(key=lambda r: -r["waarschijnlijkheid"])
    # Eén regel per (whisky, kandidaat); oudere keuzes van Erwin blijven staan.
    seen, rows = set(), []
    for r in review:
        key = (r["id"], r["kandidaatUrl"])
        if key not in seen:
            seen.add(key)
            rows.append(r)
    report["review"] = rows
    report["stats"] = dict(stats)
    return report


def page_texts(repo: Path) -> dict[str, list[tuple[str, str]]]:
    """Teksten van geaccepteerde extra-matches (omschrijving, notities, feiten) voor kenmerken."""
    accepted, _review, _stats = matches(repo)
    out: dict[str, list[tuple[str, str]]] = {}
    for iid, hits in accepted.items():
        for _source, info, _how in hits:
            text = "\n".join(x for x in (info.get("omschrijving"), info.get("notities"), spec_text(info)) if x)
            if text:
                out.setdefault(iid, []).append((info["url"], text))
    return out


def write_review(repo: Path, rows: list[dict]) -> None:
    payload = {
        "uitleg": (
            "Kandidaten die net niet door de strikte check kwamen. Zet 'goedgekeurd' op true om een "
            "koppeling te gebruiken, of op false om hem te verwerpen; null = nog niet bekeken. "
            "De cascade (scripts/enrich_extra.py) gebruikt alleen regels met goedgekeurd: true."
        ),
        "kandidaten": [
            {k: r[k] for k in ("id", "whisky", "bron", "kandidaatNaam", "kandidaatUrl", "verschil", "waarschijnlijkheid", "goedgekeurd")}
            for r in rows
        ],
    }
    es.save_json(repo / "data" / "match-review.json", payload)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--sources", default=",".join(ALL_SOURCES))
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args(argv)
    sources = [x for x in args.sources.split(",") if x]
    if args.fetch:
        print("Ophalen:", dict(fetch(args.repo, sources, args.max_pages)), file=sys.stderr)
    if args.apply:
        repo = args.repo
        descriptions = es.load_descriptions(repo / "descriptions.json")
        notes = es.load_descriptions(repo / "notes.json")
        facts = es.load_descriptions(repo / "facts.json")
        report = apply(repo, descriptions, notes, facts, sources)
        es.save_json(repo / "descriptions.json", descriptions)
        es.save_json(repo / "notes.json", notes)
        es.save_json(repo / "facts.json", facts)
        write_review(repo, report["review"])
        es.save_json(REPORT, report)
        print(report["stats"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
