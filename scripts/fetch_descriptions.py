#!/usr/bin/env python3
"""Haal productomschrijvingen en afbeeldings-URL's op van Whiskysite.nl.

Ruwe HTML wordt gecached (standaard in /workspace/whisky_build/wscache/),
zodat een hernieuwde run alleen ontbrekende pagina's ophaalt. De tekst
wordt letterlijk overgenomen, alleen getrimd en zonder navigatie of
bestel-/cookie-teksten. Alleen volume+ABV telt als te generiek; dan
wordt (indien aanwezig) de flespagina op Whiskysite geprobeerd.
Productfoto's worden niet hier bepaald; gebruik scripts/fetch_images.py
(flespagina, Whiskysite-zoeken, Whiskybase, daarna non-generieke samplefoto).
Bestaande image-velden in descriptions.json blijven behouden.

Voorbeeld:
    python scripts/fetch_descriptions.py
    python scripts/fetch_descriptions.py --data data.json --cache /workspace/whisky_build/wscache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_CACHE = Path("/workspace/whisky_build/wscache")
USER_AGENT = (
    "Mozilla/5.0 (compatible; whisky-overzicht/1.0; +https://github.com/erwindevlieg/whiskyproevers)"
)
GENERIC_RE = re.compile(
    r"""
    ^(?:
        \d+(?:[.,]\d+)?\s*(?:ltr|l\b|cl|ml)\.?
        |\d+(?:[.,]\d+)?\s*%
        |[,;/.\-\s]
    )+$
    """,
    re.IGNORECASE | re.VERBOSE,
)
BOILERPLATE_RE = re.compile(
    r"""(?ix)
    ^\s*(
        lees\ meer\.?\.?|
        je moet minimaal\s+18\s+jaar|
        .*cookie.*|
        .*voor\s+\d+\s*uur\s+besteld.*|
        .*gratis\s+verzending.*|
        .*bestel\s+(nu|voor).*|
        .*retourneren.*|
        productomschrijving
    )\s*$
    """,
)
WHISKYSITE_HOST = re.compile(r"(^|\.)whiskysite\.nl$", re.I)


def is_whiskysite(url):
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    host = host[4:] if host.startswith("www.") else host
    return bool(WHISKYSITE_HOST.search(host))


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / (digest + ".html")


def fetch_html(session: requests.Session, url: str, cache_dir: Path, delay: float, retries: int):
    path = cache_path(cache_dir, url)
    if path.is_file() and path.stat().st_size > 500:
        return path.read_text(encoding="utf-8", errors="replace"), "cache"
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            time.sleep(delay)
            response = session.get(url, timeout=30)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = "HTTP %s" % response.status_code
                time.sleep(delay * attempt * 2)
                continue
            if response.status_code >= 400:
                return None, "HTTP %s" % response.status_code
            encoding = response.apparent_encoding or response.encoding or "utf-8"
            try:
                text = response.content.decode(encoding, errors="replace")
            except LookupError:
                text = response.content.decode("utf-8", errors="replace")
            path.write_text(text, encoding="utf-8")
            # sidecar with source url for debugging
            meta = path.with_suffix(".url")
            meta.write_text(url + "\n", encoding="utf-8")
            return text, "fetch"
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(delay * attempt * 2)
    return None, last_error or "onbekende fout"


MOJIBAKE_MARKERS = {
    "â€™": "’",
    "â€˜": "‘",
    "â€œ": "“",
    "â€": "”",
    "â€“": "–",
    "â€”": "—",
    "â€¦": "…",
}


def fix_mojibake(text: str) -> str:
    """Herstel bekende UTF-8-mojibake (zoals 'Itâ€™s' → 'It’s'), zonder de tekst te herschrijven."""
    for broken, fixed in MOJIBAKE_MARKERS.items():
        if broken in text:
            text = text.replace(broken, fixed)
    return text


def clean_description(text: str) -> str:
    if not text:
        return ""
    text = fix_mojibake(text)
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if BOILERPLATE_RE.match(line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def is_generic(text: str) -> bool:
    if not text:
        return True
    compact = re.sub(r"\s+", " ", text).strip()
    if GENERIC_RE.match(compact):
        return True
    # Zeer korte meetwaarden zonder verdere productinfo.
    if len(compact) < 20 and re.fullmatch(r"[\d.,%\scla-z/]+", compact, re.I):
        return True
    return False


def extract_description(html: str) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    block = soup.select_one("#information .block-content, #information .content")
    if block is None:
        block = soup.select_one("#information")
    if block is None:
        return None
    for tag in block.select("script, style"):
        tag.decompose()
    for link in block.select('a[href="#information"], a[data-scrollview-id="information"]'):
        link.decompose()
    for br in block.find_all("br"):
        br.replace_with("\n")
    text = block.get_text("\n")
    text = clean_description(text)
    return text or None


def collect_targets(items):
    """Per rij: eerst sample-URL, daarna eventuele fles-URL op Whiskysite."""
    rows = []
    unique_urls = []
    seen = set()
    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        links = []
        for sample in item.get("samples") or []:
            url = sample.get("sampleLink")
            if is_whiskysite(url) and url not in links:
                links.append(url)
        top = item.get("sampleLink")
        if is_whiskysite(top) and top not in links:
            links.insert(0, top)
        sample = links[0] if links else None
        fles = item.get("flesLink") if is_whiskysite(item.get("flesLink")) else None
        if fles and fles == sample:
            fles = None
        if not sample and not fles:
            continue
        rows.append({
            "id": item_id,
            "sample": sample,
            "extra_samples": links[1:],
            "fles": fles,
            "aliases": item.get("aliases") or [],
        })
        for url in links + [fles]:
            if url and url not in seen:
                seen.add(url)
                unique_urls.append(url)
    return rows, unique_urls


IMAGE_URL_RE = re.compile(r"^https://[^\s]+\.(?:jpe?g|png|webp)(?:\?.*)?$", re.I)


def extract_image(html: str) -> str | None:
    """Productfoto-URL uit og:image, anders de zichtbare productafbeelding."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    def usable(url: str | None) -> str | None:
        if not url:
            return None
        url = url.strip()
        if not IMAGE_URL_RE.match(url):
            return None
        lower = url.lower()
        if "logo" in lower or "favicon" in lower or "placeholder" in lower:
            return None
        return url

    og = soup.find("meta", attrs={"property": re.compile(r"^og:image$", re.I)})
    if og:
        found = usable(og.get("content"))
        if found:
            return found
    item = soup.find(attrs={"itemprop": re.compile(r"^image$", re.I)})
    if item:
        found = usable(item.get("content") or item.get("src"))
        if found:
            return found
    img = soup.select_one("img.product-image, .product-image img, #image img, .image img")
    if img:
        found = usable(img.get("src") or img.get("data-src"))
        if found:
            return found
    return None


def pick_image(sample_image, fles_image):
    return sample_image or fles_image or None


def pick_text(sample_text, sample_url, fles_text, fles_url):
    if sample_text and not is_generic(sample_text):
        return sample_text, sample_url
    if fles_text and not is_generic(fles_text):
        return fles_text, fles_url
    return None, None


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Haal Whiskysite-productomschrijvingen en afbeeldings-URL's op en schrijf descriptions.json."
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Pad naar data.json (standaard: data.json in de repo).",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help="Map voor ruwe HTML-cache (standaard: /workspace/whisky_build/wscache).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Uitvoerbestand (standaard: descriptions.json naast data.json).",
    )
    parser.add_argument("--delay", type=float, default=0.7, help="Pauze tussen netwerkrequests in seconden.")
    parser.add_argument("--retries", type=int, default=3, help="Aantal pogingen per URL.")
    parser.add_argument("--limit", type=int, default=0, help="Optioneel: max. aantal unieke URL's (voor tests).")
    args = parser.parse_args(argv)

    data_path = (args.data or default_data_path()).expanduser().resolve()
    if not data_path.is_file():
        raise SystemExit("data.json niet gevonden: %s" % data_path)
    cache_dir = args.cache.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = args.output
    if output is None:
        output = data_path.parent / "descriptions.json"
    else:
        output = output.expanduser().resolve()

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    rows, urls = collect_targets(items)
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl,en;q=0.8"})

    html_by_url = {}
    fetched = cached = failed = 0
    for index, url in enumerate(urls, start=1):
        html, status = fetch_html(session, url, cache_dir, args.delay, args.retries)
        if html:
            html_by_url[url] = html
            if status == "cache":
                cached += 1
            else:
                fetched += 1
        else:
            failed += 1
            print("Mislukt: %s (%s)" % (url, status), file=sys.stderr)
        if index == 1 or index % 25 == 0 or index == len(urls):
            print(
                "URL %s/%s  nieuw=%s  cache=%s  fout=%s" % (index, len(urls), fetched, cached, failed),
                file=sys.stderr,
                flush=True,
            )

    texts = {url: extract_description(html) for url, html in html_by_url.items()}

    existing = {}
    if output.is_file():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

    descriptions = {}
    used_sample = used_fles = 0
    with_image = 0
    for row in rows:
        sample_text = texts.get(row["sample"]) if row["sample"] else None
        sample_source = row["sample"]
        for extra in row.get("extra_samples") or []:
            extra_text = texts.get(extra)
            if extra_text and (not sample_text or len(extra_text) > len(sample_text)):
                sample_text = extra_text
                sample_source = extra
        fles_text = texts.get(row["fles"]) if row["fles"] else None
        text, source = pick_text(sample_text, sample_source, fles_text, row["fles"])
        prev = existing.get(row["id"]) or {}
        if not prev:
            for alias in row.get("aliases") or []:
                if existing.get(alias):
                    prev = existing[alias]
                    break
        image = prev.get("image")
        image_source = prev.get("imageSource")
        if not text and not image:
            if prev:
                descriptions[row["id"]] = prev
            continue
        entry = dict(prev)
        if text and source:
            entry["text"] = text
            entry["sourceUrl"] = source
            if source == row["sample"]:
                used_sample += 1
            else:
                used_fles += 1
        if image:
            entry["image"] = image
            if image_source:
                entry["imageSource"] = image_source
            with_image += 1
        descriptions[row["id"]] = entry
    # behoud rijen die alleen een foto hebben en geen Whiskysite-tekstdoel
    for item_id, prev in existing.items():
        if item_id not in descriptions and (prev.get("image") or prev.get("text")):
            descriptions[item_id] = prev
            if prev.get("image"):
                with_image += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(descriptions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    missing = len(rows) - used_sample - used_fles
    print(
        "Geschreven: %s  gevonden=%s (sample=%s, fles=%s)  zonder omschrijving=%s  "
        "afbeeldingen=%s (bestaande, zie fetch_images.py)  rijen-met-link=%s  urls=%s"
        % (
            output,
            used_sample + used_fles,
            used_sample,
            used_fles,
            missing,
            with_image,
            len(rows),
            len(urls),
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
