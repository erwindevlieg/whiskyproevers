#!/usr/bin/env python3
"""Bepaal productfoto-URL's voor data.json en schrijf ze in descriptions.json.

Alleen URL's worden opgeslagen, geen beeldbestanden. Bronnen, in volgorde:

1. Flespagina: Whiskysite-fleslink indien aanwezig, anders een andere
   winkelfles-URL (flesLink of tweedeLink).
2. Als er geen fles-URL is (of die geen bruikbare foto geeft): een
   overeenkomende fles op Whiskysite via zoeken, alleen bij een zekere
   naam-match (zelfde distilleerderij, leeftijd, editie/batch; geen sample).
3. Whiskybase-flesfoto bij WB-status exact of labelversie (eerst wbcache,
   daarna eventueel live). Bij een Cloudflare-blokkade wordt live
   overgeslagen, zonder omwegen.
4. Pas daarna de samplepagina-foto, tenzij die een generieke sample-fles
   (het 3/6 cl-sampleplaatje) is.

Caches (niet committen) staan standaard onder /workspace/whisky_build/.

Voorbeeld:
    python scripts/fetch_images.py
    python scripts/fetch_images.py --data data.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_BUILD = Path("/workspace/whisky_build")
USER_AGENT = (
    "Mozilla/5.0 (compatible; whisky-overzicht/1.0; +https://github.com/erwindevlieg/whiskyproevers)"
)
IMAGE_URL_RE = re.compile(r"^https://[^\s]+", re.I)
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?.*)?$", re.I)
WHISKYSITE_HOST = re.compile(r"(^|\.)whiskysite\.nl$", re.I)
SAMPLE_MARK_RE = re.compile(
    r"(?:^|[\s\-_/])(?:\d+\s*cl\s+)?sample(?:s)?(?:$|[\s\-_/])|placeholder|[\s\-_/]vial[\s\-_/]",
    re.I,
)
VOLUME_RE = re.compile(
    r"""
    \b(?:
        0[,.]?70\s*(?:ltr|l)\b
        |070(?:-ltr)?
        |\d+(?:[.,]\d+)?\s*(?:cl|ml|ltr|l)\b
    )
    """,
    re.I | re.VERBOSE,
)
ABV_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*%")
AGE_PHRASE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:years?\s*old|jaar(?:\s*oud)?|yo)\b",
    re.I,
)
THE_RE = re.compile(r"^\s*(?:the|de|het|a|an)\s+", re.I)
STOP_HOSTS = {
    "whiskyfun.com",
    "distiller.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
}
WB_OK_STATUS = {"exact", "labelversie"}
GENERIC_DIMS = {(450, 472)}
WS_SIZE_PATH_RE = re.compile(r"/\d+x\d+x\d+/")


def is_whiskysite(url: str | None) -> bool:
    host = host_of(url)
    return bool(host and WHISKYSITE_HOST.search(host))


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / (digest + ".html")


def write_sidecar(path: Path, url: str) -> None:
    path.with_suffix(".url").write_text(url + "\n", encoding="utf-8")


def fetch_html(
    session: requests.Session,
    url: str,
    cache_dir: Path,
    delay: float,
    retries: int,
    *,
    allow_live: bool = True,
):
    """Return (html, status). status is cache, fetch, HTTP n, blocked, or an error string."""
    path = cache_path(cache_dir, url)
    if path.is_file() and path.stat().st_size > 500:
        return path.read_text(encoding="utf-8", errors="replace"), "cache"
    if not allow_live:
        return None, "geen-cache"
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(delay)
            response = session.get(url, timeout=30)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = "HTTP %s" % response.status_code
                time.sleep(delay * attempt * 2)
                continue
            if is_cloudflare_block(response):
                print("Geblokkeerd: %s (HTTP %s)" % (url, response.status_code), file=sys.stderr, flush=True)
                return None, "blocked"
            if response.status_code >= 400:
                return None, "HTTP %s" % response.status_code
            encoding = response.apparent_encoding or response.encoding or "utf-8"
            try:
                text = response.content.decode(encoding, errors="replace")
            except LookupError:
                text = response.content.decode("utf-8", errors="replace")
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            write_sidecar(path, url)
            return text, "fetch"
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(delay * attempt * 2)
    return None, last_error or "onbekende fout"


def is_cloudflare_block(response: requests.Response) -> bool:
    """Alleen echte challenge-pagina's of een 403 van Whiskybase, niet elke Cloudflare-CDN-response."""
    text_head = ""
    try:
        text_head = response.text[:2500].lower()
    except Exception:
        text_head = ""
    if "attention required" in text_head and "cloudflare" in text_head:
        return True
    if "cf-browser-verification" in text_head or "cdn-cgi/challenge" in text_head:
        return True
    if "just a moment" in text_head and "cloudflare" in text_head:
        return True
    host = ""
    try:
        host = urlparse(response.url or "").netloc.lower()
    except Exception:
        host = ""
    if response.status_code in (401, 403) and "whiskybase.com" in host:
        return True
    return False


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.lower()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def search_query(naam: str) -> str:
    """Naam zonder sample-inhoud en losse ABV, geschikt als Whiskysite-zoekterm."""
    if not naam:
        return ""
    text = re.sub(r"\s+\d+\s*CL\s+Sample\s*$", "", naam, flags=re.I)
    text = re.sub(r"[\(\[]\s*\d+(?:[.,]\d+)?\s*%\s*[\)\]]", " ", text)
    text = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%", " ", text)
    text = text.replace("'", "").replace("’", "").replace("`", "")
    text = text.replace("&", " ").replace("#", " ").replace("/", " ")
    text = re.sub(r"[()[\]{}]", " ", text)
    return collapse(text)


def identity(text: str) -> str:
    """Kern van een naam: distilleerderij, leeftijd en editie, zonder volume/ABV/sample."""
    if not text:
        return ""
    t = fold(text)
    t = t.replace("&", " and ")
    t = ABV_RE.sub(" ", t)
    t = VOLUME_RE.sub(" ", t)
    t = re.sub(r"\b0\s+70\s*(?:ltr|l)\b", " ", t)
    t = AGE_PHRASE_RE.sub(r"\1", t)
    t = SAMPLE_MARK_RE.sub(" ", t)
    t = re.sub(r"[/_,+]+", " ", t)
    t = re.sub(r"[^a-z0-9\s.-]", " ", t)
    t = THE_RE.sub("", t)
    t = re.sub(r"\b(?:ltr|liter|litre)\b", " ", t)
    return collapse(t)


def is_sample_product(title: str | None, url: str | None) -> bool:
    blob = " ".join(p for p in (title or "", url or "") if p)
    if SAMPLE_MARK_RE.search(blob):
        return True
    if re.search(r"\d+\s*-?cl-?sample", blob, re.I):
        return True
    return False


def upgrade_ws_image(url: str) -> str:
    if not url:
        return url
    return WS_SIZE_PATH_RE.sub("/", url)


def looks_like_image_url(url: str | None) -> bool:
    if not url:
        return False
    url = url.strip()
    if not IMAGE_URL_RE.match(url):
        return False
    lower = url.lower()
    if any(x in lower for x in ("logo", "favicon", "sprite", "icon-", "/icons/", "payment", "flag-")):
        return False
    if IMAGE_EXT_RE.search(url.split("?")[0]) or "og:image" in lower:
        return True
    # sommige shops zetten geen extensie in og:image
    if "/image" in lower or "/media" in lower or "/product" in lower or "cdn." in lower:
        return True
    return bool(IMAGE_EXT_RE.search(url))


def extract_image(html: str, *, whiskybase: bool = False) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    def usable(url: str | None) -> str | None:
        if not looks_like_image_url(url):
            return None
        url = url.strip()
        if "placeholder" in url.lower():
            return None
        return upgrade_ws_image(url) if "webshopapp.com" in url else url

    for prop in ("og:image", "og:image:url", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag:
            found = usable(tag.get("content"))
            if found:
                return found
    item = soup.find(attrs={"itemprop": re.compile(r"^image$", re.I)})
    if item:
        found = usable(item.get("content") or item.get("src"))
        if found:
            return found
    if whiskybase:
        img = soup.select_one('img[src*="static.whiskybase.com"], img[src*="whiskybase.com"][src*="whisk"]')
        if img:
            found = usable(img.get("src") or img.get("data-src"))
            if found:
                return found
    img = soup.select_one(
        "img.product-image, .product-image img, #image img, .image img, "
        ".product-block-image img, .product__photo img, .product-photo img"
    )
    if img:
        found = usable(img.get("src") or img.get("data-src"))
        if found:
            return found
    return None


def extract_title(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", attrs={"property": re.compile(r"^og:title$", re.I)})
    if og and og.get("content"):
        return collapse(og.get("content"))
    h1 = soup.find("h1")
    if h1:
        return collapse(h1.get_text(" ", strip=True))
    if soup.title and soup.title.string:
        return collapse(soup.title.string)
    return ""


def parse_jpeg_size(data: bytes):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            return None
        typ = data[i + 1]
        if typ in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        if typ in (0xD8, 0xD9, 0x01) or 0xD0 <= typ <= 0xD7:
            i += 2
            continue
        if i + 4 > len(data):
            return None
        seglen = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seglen
    return None


def parse_png_size(data: bytes):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    return None


def parse_webp_size(data: bytes):
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    if kind == b"VP8 " and len(data) >= 30:
        # start code 0x9d012a at offset 23
        idx = data.find(b"\x9d\x01\x2a")
        if idx != -1 and idx + 7 <= len(data):
            w = struct.unpack("<H", data[idx + 3 : idx + 5])[0] & 0x3FFF
            h = struct.unpack("<H", data[idx + 5 : idx + 7])[0] & 0x3FFF
            return w, h
    if kind == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    return None


def parse_image_size(data: bytes):
    return parse_jpeg_size(data) or parse_png_size(data) or parse_webp_size(data)


def url_filename_generic(url: str) -> bool:
    path = urlparse(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    if "placeholder" in path or "no-image" in path or "nophoto" in path:
        return True
    if SAMPLE_MARK_RE.search(name) or SAMPLE_MARK_RE.search(path):
        return True
    return False


def image_meta(
    session: requests.Session,
    url: str,
    meta_dir: Path,
    delay: float,
    retries: int,
):
    """Cached (width, height, length, generic_hint). Missing fields may be None."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    path = meta_dir / (digest + ".json")
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    meta = {"url": url, "width": None, "height": None, "length": None}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(delay * 0.25)
            headers = {"Range": "bytes=0-16383", "User-Agent": USER_AGENT}
            response = session.get(url, timeout=20, headers=headers)
            if response.status_code >= 400 and response.status_code != 206:
                last_error = "HTTP %s" % response.status_code
                if response.status_code in (401, 403):
                    break
                time.sleep(delay * attempt)
                continue
            data = response.content[:16384]
            cr = response.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)$", cr)
            if m:
                meta["length"] = int(m.group(1))
            elif response.headers.get("Content-Length") and response.status_code == 200:
                meta["length"] = int(response.headers["Content-Length"])
            else:
                meta["length"] = len(response.content)
            size = parse_image_size(data)
            if size:
                meta["width"], meta["height"] = size
            break
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(delay * attempt)
    if last_error and meta["width"] is None:
        meta["error"] = last_error
    meta_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")
    return meta


def is_generic_vial(url: str, meta: dict | None) -> bool:
    if not url:
        return True
    if url_filename_generic(url):
        return True
    if meta:
        w, h = meta.get("width"), meta.get("height")
        if w and h and (w, h) in GENERIC_DIMS:
            return True
    return False


def accept_image(
    session: requests.Session,
    url: str | None,
    meta_dir: Path,
    delay: float,
    retries: int,
    *,
    require_meta: bool = False,
) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url.startswith("https://"):
        return None
    if url_filename_generic(url) and require_meta:
        # sample-pagina met 'sample' in de bestandsnaam: vrijwel altijd het flesje
        return None
    if url_filename_generic(url) and not require_meta:
        # ook op een flespagina geen sample-graphic gebruiken
        return None
    if require_meta or True:
        meta = image_meta(session, url, meta_dir, delay, retries)
        if is_generic_vial(url, meta):
            return None
        if require_meta and (not meta.get("width") or not meta.get("height")):
            # liever geen foto dan per ongeluk het sample-flesje
            return None
    return url


def is_shop_product_url(url: str | None) -> bool:
    if not url or not url.startswith("http"):
        return False
    host = host_of(url)
    if not host or host in STOP_HOSTS:
        return False
    if "whiskybase.com" == host:
        # review/collectiepagina, geen shop
        return False
    if is_sample_product(None, url) and is_whiskysite(url):
        return False
    return True


def bottle_urls_for(item: dict) -> list[str]:
    urls = []
    for field in ("flesLink", "tweedeLink"):
        url = item.get(field)
        if is_shop_product_url(url) and url not in urls:
            # tweedeLink alleen als het een winkelproduct lijkt, geen sample van Whiskysite
            if field == "tweedeLink" and is_whiskysite(url) and is_sample_product(None, url):
                continue
            urls.append(url)
    return urls


def whiskybase_id(item: dict) -> str | None:
    raw = (item.get("wbId") or "").strip()
    if raw:
        m = re.search(r"(\d+)", raw)
        if m:
            return m.group(1)
    link = item.get("wbLink") or ""
    m = re.search(r"/whiskies/whisky/(\d+)/", link)
    if m:
        return m.group(1)
    return None


def parse_search_results(html: str) -> list[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for holder in soup.select(".product-block-holder, .product-block"):
        link = holder.select_one("a.title[href]") or holder.select_one("h4 a[href]") or holder.select_one(".product-block-image a[href]")
        if not link:
            continue
        href = link.get("href") or ""
        if href.startswith("/"):
            href = "https://www.whiskysite.nl" + href
        href = href.split("?")[0]
        if not href or href in seen or "/search" in href:
            continue
        title = collapse(link.get("title") or link.get_text(" ", strip=True) or "")
        if not title:
            img_alt = holder.select_one("img[alt], img[title]")
            if img_alt:
                title = collapse(img_alt.get("alt") or img_alt.get("title") or "")
        img = holder.select_one("img[src], a[data-image]")
        image = None
        if img:
            image = img.get("src") or img.get("data-src") or img.get("data-image")
        if not image:
            qa = holder.select_one("[data-image]")
            if qa:
                image = qa.get("data-image")
        if image:
            image = upgrade_ws_image(image)
        seen.add(href)
        results.append(
            {
                "url": href,
                "title": title,
                "image": image,
                "is_sample": is_sample_product(title, href),
                "identity": identity(title),
            }
        )
    return results


def prefer_bottle(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    def score(item):
        blob = " ".join((item.get("title") or "", item.get("url") or "")).lower()
        s = 0
        if re.search(r"0[,.]?70|070-ltr|70\s*cl|70cl", blob):
            s += 3
        if item.get("image"):
            s += 1
        return s
    return sorted(candidates, key=score, reverse=True)[0]


def match_confident(query_name: str, results: list[dict]) -> dict | None:
    want = identity(query_name)
    if not want or len(want) < 4:
        return None
    hits = [
        r
        for r in results
        if not r.get("is_sample") and r.get("identity") == want
    ]
    return prefer_bottle(hits)


def ws_search_url(query: str) -> str:
    q = quote(query, safe=" ").replace(" ", "+")
    return "https://www.whiskysite.nl/nl/search/" + q + "/"


def default_data_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "data.json",
        Path.cwd() / "data.json",
        Path("/workspace/whiskyproevers/data.json"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_ws_index(cache_dir: Path) -> list[dict]:
    index = []
    if not cache_dir.is_dir():
        return index
    for url_path in cache_dir.glob("*.url"):
        html_path = url_path.with_suffix(".html")
        if not html_path.is_file():
            continue
        url = url_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        if not is_whiskysite(url):
            continue
        html = html_path.read_text(encoding="utf-8", errors="replace")
        title = extract_title(html)
        image = extract_image(html)
        index.append(
            {
                "url": url,
                "title": title,
                "image": image,
                "is_sample": is_sample_product(title, url),
                "identity": identity(title) or identity(url),
            }
        )
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bepaal productfoto-URL's en schrijf ze naar descriptions.json."
    )
    parser.add_argument("--data", type=Path, default=None, help="Pad naar data.json.")
    parser.add_argument(
        "--descriptions",
        type=Path,
        default=None,
        help="descriptions.json (wordt bijgewerkt, bestaande teksten blijven).",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=DEFAULT_BUILD,
        help="Basismap voor caches (wscache, shopcache, wsearch, wbcache, imgmeta).",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="Pauze tussen netwerkrequests.")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="Max. aantal rijen (testen).")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--skip-shops", action="store_true")
    parser.add_argument("--skip-whiskybase", action="store_true")
    args = parser.parse_args(argv)

    data_path = (args.data or default_data_path()).expanduser().resolve()
    if not data_path.is_file():
        raise SystemExit("data.json niet gevonden: %s" % data_path)
    desc_path = args.descriptions
    if desc_path is None:
        desc_path = data_path.parent / "descriptions.json"
    else:
        desc_path = desc_path.expanduser().resolve()

    build_dir = args.build_dir.expanduser().resolve()
    ws_cache = build_dir / "wscache"
    shop_cache = build_dir / "shopcache"
    search_cache = build_dir / "wsearch"
    wb_cache = build_dir / "wbcache"
    img_meta = build_dir / "imgmeta"
    for folder in (ws_cache, shop_cache, search_cache, wb_cache, img_meta):
        folder.mkdir(parents=True, exist_ok=True)

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if args.limit and args.limit > 0:
        items = items[: args.limit]

    descriptions = {}
    if desc_path.is_file():
        descriptions = json.loads(desc_path.read_text(encoding="utf-8"))
        if not isinstance(descriptions, dict):
            descriptions = {}

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    print("Indexeren van Whiskysite-cache…", file=sys.stderr, flush=True)
    ws_index = load_ws_index(ws_cache)
    print("  %s gecachete Whiskysite-pagina's" % len(ws_index), file=sys.stderr, flush=True)

    def html_for(url: str, cache: Path, allow_live=True):
        return fetch_html(session, url, cache, args.delay, args.retries, allow_live=allow_live)

    # Prefetch andere winkelfles-URL's
    shop_urls = []
    seen_shops = set()
    if not args.skip_shops:
        for item in items:
            for url in bottle_urls_for(item):
                if is_whiskysite(url):
                    continue
                if url not in seen_shops:
                    seen_shops.add(url)
                    shop_urls.append(url)
    fetched = cached = failed = 0
    for i, url in enumerate(shop_urls, start=1):
        html, status = html_for(url, shop_cache)
        if html:
            if status == "cache":
                cached += 1
            else:
                fetched += 1
        else:
            failed += 1
            print("Winkel mislukt: %s (%s)" % (url, status), file=sys.stderr)
        if i == 1 or i % 20 == 0 or i == len(shop_urls):
            print(
                "Winkels %s/%s  nieuw=%s  cache=%s  fout=%s"
                % (i, len(shop_urls), fetched, cached, failed),
                file=sys.stderr,
                flush=True,
            )

    search_memo: dict[str, list[dict]] = {}
    wb_live_blocked = False
    search_live_blocked = False
    search_block_streak = 0
    counts = {
        "bottle-whiskysite": 0,
        "bottle-shop": 0,
        "whiskysite-search": 0,
        "drankdozijn": 0,
        "whiskybase": 0,
        "sample": 0,
        "none": 0,
    }

    dd_dir = build_dir / "ddcache"
    dd_by_id = {}
    dd_by_url = {}
    dd_images_path = dd_dir / "found-images.json"
    dd_prices_path = dd_dir / "found-prices.json"
    if dd_images_path.is_file():
        raw = json.loads(dd_images_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, val in raw.items():
                if isinstance(val, dict) and val.get("image"):
                    dd_by_id[key] = val
                    if val.get("url"):
                        dd_by_url[val["url"]] = val
    if dd_prices_path.is_file():
        raw = json.loads(dd_prices_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for val in raw:
                if val.get("image") and val.get("url"):
                    dd_by_url[val["url"]] = val
                    if val.get("id"):
                        dd_by_id[val["id"]] = val
    print("  Drankdozijn-cache: %s foto's (id) / %s URL's" % (len(dd_by_id), len(dd_by_url)), file=sys.stderr, flush=True)

    def accept(url, *, require_meta=False):
        return accept_image(
            session, url, img_meta, args.delay, args.retries, require_meta=require_meta
        )

    def image_from_page(url: str):
        cache = ws_cache if is_whiskysite(url) else shop_cache
        html, status = html_for(url, cache)
        if not html:
            return None, status
        return extract_image(html), status

    def bottle_page_image(item: dict):
        for url in bottle_urls_for(item):
            image, _status = image_from_page(url)
            if not image and url in dd_by_url:
                image = dd_by_url[url].get("image")
            accepted = accept(image, require_meta=False) if (url in dd_by_url and image) else accept(image, require_meta=True)
            if accepted:
                host = host_of(url) or ""
                if is_whiskysite(url):
                    source = "bottle-whiskysite"
                elif "drankdozijn.nl" in host:
                    source = "drankdozijn"
                else:
                    source = "bottle-shop"
                return accepted, source
        return None, None

    def drankdozijn_image(item: dict):
        """Gecachete Drankdozijn-foto (via gerenderde pagina), alleen bij bekende id/url."""
        entry = dd_by_id.get(item.get("id") or "")
        if not entry:
            return None
        return accept(entry.get("image"), require_meta=False)

    def search_image(item: dict):
        nonlocal search_live_blocked, search_block_streak
        query = search_query(item.get("naam") or "")
        if not query:
            return None
        # eerst in de bestaande cache van productpagina's
        want = identity(query)
        cached_hits = [
            r
            for r in ws_index
            if not r.get("is_sample") and r.get("identity") == want
        ]
        picked = prefer_bottle(cached_hits)
        if picked and picked.get("image"):
            accepted = accept(picked["image"], require_meta=True)
            if accepted:
                return accepted
        if args.skip_search or search_live_blocked:
            return None
        if query not in search_memo:
            url = ws_search_url(query)
            html, status = html_for(url, search_cache)
            if status == "blocked":
                search_memo[query] = []
                plain = bool(re.fullmatch(r"[A-Za-z0-9 ]+", query or ""))
                if plain:
                    search_block_streak += 1
                print("Whiskysite-zoeken mislukt voor %r (%s, plain=%s, reeks=%s)" % (query, status, plain, search_block_streak), file=sys.stderr, flush=True)
                if plain and search_block_streak >= 5:
                    search_live_blocked = True
                    print("Whiskysite-zoeken herhaaldelijk geblokkeerd; verdere live zoekopdrachten overgeslagen.", file=sys.stderr, flush=True)
                return None
            if status and status.startswith("HTTP"):
                search_memo[query] = []
                print("Whiskysite-zoeken HTTP-fout voor %r (%s)" % (query, status), file=sys.stderr, flush=True)
                return None
            search_block_streak = 0
            search_memo[query] = parse_search_results(html or "")
        picked = match_confident(query, search_memo[query])
        if not picked:
            return None
        image = picked.get("image")
        # liever de og:image van de flespagina (vaak al of nog niet in cache)
        if picked.get("url"):
            page_image, _ = image_from_page(picked["url"])
            if page_image:
                image = page_image
            # voeg gevonden flespagina toe aan de index voor zusterrijen
            if page_image or picked.get("title"):
                ws_index.append(
                    {
                        "url": picked["url"],
                        "title": picked.get("title") or "",
                        "image": page_image or picked.get("image"),
                        "is_sample": False,
                        "identity": identity(picked.get("title") or query),
                    }
                )
        return accept(image, require_meta=True)

    def whiskybase_image(item: dict):
        nonlocal wb_live_blocked
        if args.skip_whiskybase:
            return None
        status = (item.get("wbStatus") or "").strip().lower()
        if status not in WB_OK_STATUS:
            return None
        wb_id = whiskybase_id(item)
        if not wb_id:
            return None
        html_path = wb_cache / (wb_id + ".html")
        html = None
        if html_path.is_file() and html_path.stat().st_size > 500:
            html = html_path.read_text(encoding="utf-8", errors="replace")
        else:
            # JSON-cache bevat geen fotovelden; niet als HTML lezen.
            link = (item.get("wbLink") or "").strip()
            if not link and wb_id:
                link = "https://www.whiskybase.com/whiskies/whisky/%s/" % wb_id
            if not link or wb_live_blocked:
                return None
            page, st = fetch_html(session, link, wb_cache, args.delay, args.retries)
            # sla HTML ook op als {id}.html zodat herhaalde runs de JSON niet verwarren
            if page:
                html = page
                # fetch_html schreef sha1.html; kopieer/link naar id.html
                if not html_path.is_file():
                    html_path.write_text(page, encoding="utf-8")
            elif st == "blocked":
                wb_live_blocked = True
                print(
                    "Whiskybase geblokkeerd (Cloudflare/HTTP); verdere live verzoeken overgeslagen.",
                    file=sys.stderr,
                    flush=True,
                )
                return None
        if not html:
            return None
        image = extract_image(html, whiskybase=True)
        return accept(image, require_meta=True)

    def sample_image(item: dict):
        url = item.get("sampleLink")
        if not is_whiskysite(url):
            return None
        image, _ = image_from_page(url)
        return accept(image, require_meta=True)

    total = len(items)
    for i, item in enumerate(items, start=1):
        item_id = item.get("id")
        image = source = None
        image, source = bottle_page_image(item)
        if not image:
            image = search_image(item)
            if image:
                source = "whiskysite-search"
        if not image:
            image = drankdozijn_image(item)
            if image:
                source = "drankdozijn"
        if not image:
            image = whiskybase_image(item)
            if image:
                source = "whiskybase"
        if not image:
            image = sample_image(item)
            if image:
                source = "sample"
        if image and source:
            counts[source] = counts.get(source, 0) + 1
            entry = descriptions.get(item_id) or {}
            entry["image"] = image
            entry["imageSource"] = source
            descriptions[item_id] = entry
        else:
            counts["none"] += 1
            entry = descriptions.get(item_id)
            if entry:
                entry.pop("image", None)
                entry.pop("imageSource", None)
                if entry:
                    descriptions[item_id] = entry
                else:
                    descriptions.pop(item_id, None)
        if i == 1 or i % 50 == 0 or i == total:
            print(
                "Rij %s/%s  fles-ws=%s  fles-winkel=%s  zoek=%s  drankdozijn=%s  wb=%s  sample=%s  geen=%s"
                % (
                    i,
                    total,
                    counts["bottle-whiskysite"],
                    counts["bottle-shop"],
                    counts["whiskysite-search"],
                    counts["drankdozijn"],
                    counts["whiskybase"],
                    counts["sample"],
                    counts["none"],
                ),
                file=sys.stderr,
                flush=True,
            )
            if i % 50 == 0:
                desc_path.write_text(
                    json.dumps(descriptions, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    desc_path.write_text(
        json.dumps(descriptions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with_image = sum(counts[k] for k in counts if k != "none")
    print(
        "Geschreven: %s  afbeeldingen=%s  flespagina=%s (whiskysite=%s, andere winkel=%s)  "
        "whiskysite-zoek=%s  drankdozijn=%s  whiskybase=%s  sample=%s  geen=%s  rijen=%s"
        % (
            desc_path,
            with_image,
            counts["bottle-whiskysite"] + counts["bottle-shop"],
            counts["bottle-whiskysite"],
            counts["bottle-shop"],
            counts["whiskysite-search"],
            counts["drankdozijn"],
            counts["whiskybase"],
            counts["sample"],
            counts["none"],
            total,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
