#!/usr/bin/env python3
"""Schrijf de compacte index, detail-chunks, thumbs en versie in index.html."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from thumbs import DEFAULT_CACHE, build_thumbs

DATA_CHUNKS = 8
SEARCH_EXTRA_FIELDS = (
    "winkel",
    "tweedeWinkel",
    "wbStatus",
    "wbReden",
    "whiskyfunScore",
    "distillerScore",
    "opmerkingFles",
    "proefnotities",
    "wbId",
)
DETAIL_FIELDS = (
    "sampleLink",
    "flesLink",
    "flesGrootte",
    "winkel",
    "opVoorraad",
    "gecontroleerdOp",
    "tweedeLink",
    "tweedePrijs",
    "tweedeWinkel",
    "opmerkingFles",
    "wbId",
    "wbLink",
    "wbStatus",
    "wbReden",
    "whiskyfunLink",
    "whiskyfunScore",
    "distillerLink",
    "distillerScore",
    "bronNotities",
    "omschrijving",
    "omschrijvingBron",
    "afbeelding",
    "aliases",
)
MARKER_RE = re.compile(
    r"/\* data-build \*/.*?/\* /data-build \*/",
    re.S,
)
MARKER_BLOCK = """/* data-build */
const DATA_BUILD = "%s";
const DATA_INDEX = "data/index-%s.json";
const DATA_CHUNK_PREFIX = "data/d/%s-";
const DATA_CHUNKS = %s;
/* /data-build */"""


def chunk_of(item_id: str, n: int = DATA_CHUNKS) -> int:
    h = 0
    for ch in item_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % n


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None and v != "" and v != []}
    return obj


def search_extra(item: dict) -> str:
    parts = []
    seen = set()
    for key in SEARCH_EXTRA_FIELDS:
        val = item.get(key)
        if val is None:
            continue
        text = str(val).strip().lower()
        if not text or text == "onbekend":
            continue
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return " ".join(parts)


def compact_item(item: dict, thumb: dict | None) -> dict:
    row = {"id": item["id"]}
    mapping = {
        "n": "naam",
        "l": "leeftijd",
        "a": "abv",
        "r": "regio",
        "v": "vattype",
        "k": "rook",
        "s": "score",
        "m": "stemmen",
        "p": "prijsSample",
        "f": "flesPrijs",
        "h": "inhoud",
    }
    for short, key in mapping.items():
        val = item.get(key)
        if val is not None and val != "":
            row[short] = val
    sb = item.get("sampleBeschikbaar")
    if sb == "ja":
        row["b"] = 1
    elif sb == "nee":
        row["b"] = 0
    elif sb:
        row["b"] = sb
    for short, key in (("c", "ncf"), ("z", "kleurstof")):
        val = item.get(key)
        if val and val != "onbekend":
            row[short] = val
    notes = item.get("proefnotities")
    if notes and notes != "onbekend":
        row["o"] = notes
    extra = search_extra(item)
    if extra:
        row["q"] = extra
    samples = item.get("samples") or []
    if len(samples) > 1:
        compact_samples = []
        for s in samples:
            rec = {}
            if s.get("inhoud"):
                rec["h"] = s["inhoud"]
            if s.get("prijsSample") is not None:
                rec["p"] = s["prijsSample"]
            compact_samples.append(rec)
        row["ss"] = compact_samples
    if thumb:
        row["t"] = 1
        row["g"] = "c" if thumb["kind"] == "clear" else "p"
        row["w"] = thumb["w"]
        row["e"] = thumb["h"]
    return _clean(row)


def detail_item(item: dict) -> dict:
    row = {}
    for key in DETAIL_FIELDS:
        if key in item and item[key] not in (None, "", []):
            row[key] = item[key]
    samples = item.get("samples") or []
    if samples:
        # Volledige sample-rijen (inclusief links) alleen in de details.
        row["samples"] = samples
    return row


def write_marker(index_html: Path, version: str) -> None:
    text = index_html.read_text(encoding="utf-8")
    block = MARKER_BLOCK % (version, version, version, DATA_CHUNKS)
    if MARKER_RE.search(text):
        text = MARKER_RE.sub(block, text, count=1)
    else:
        # Plaats het blok aan het begin van het hoofdscript.
        needle = "const VAT_ORDER ="
        if needle not in text:
            raise SystemExit("Kon DATA_BUILD niet in index.html plaatsen.")
        text = text.replace(needle, block + "\n" + needle, 1)
    index_html.write_text(text, encoding="utf-8")


def clear_old_data_files(data_dir: Path, keep_prefix: str) -> None:
    if not data_dir.is_dir():
        return
    for path in data_dir.glob("index-*.json"):
        if path.name != "index-%s.json" % keep_prefix:
            path.unlink()
    d_dir = data_dir / "d"
    if d_dir.is_dir():
        for path in d_dir.glob("*.json"):
            if not path.name.startswith(keep_prefix + "-"):
                path.unlink()


def export_site(
    payload: dict,
    root: Path,
    cache_dir: Path = DEFAULT_CACHE,
    skip_thumbs: bool = False,
    thumb_stats: dict | None = None,
) -> dict:
    root = root.resolve()
    data_dir = root / "data"
    thumbs_dir = root / "assets" / "thumbs"
    items = payload["items"]

    if thumb_stats is None and not skip_thumbs:
        thumb_stats = build_thumbs(
            items,
            thumbs_dir,
            cache_dir,
            report_path=cache_dir / "thumbs-report.json",
        )
    elif thumb_stats is None:
        thumb_stats = {"items": {}, "clear": 0, "photo": 0, "count": 0, "bytes": 0, "failed": 0}

    thumbs = thumb_stats.get("items") or {}
    index_items = [compact_item(item, thumbs.get(item["id"])) for item in items]
    chunks = [{} for _ in range(DATA_CHUNKS)]
    for item in items:
        extra = detail_item(item)
        if extra:
            chunks[chunk_of(item["id"])][item["id"]] = extra

    index_payload = {
        "generatedAt": payload.get("generatedAt"),
        "count": payload.get("count", len(index_items)),
        "redirects": payload.get("redirects") or {},
        "items": index_items,
    }
    raw_index = json.dumps(index_payload, ensure_ascii=False, separators=(",", ":"))
    raw_chunks = [
        json.dumps(ch, ensure_ascii=False, separators=(",", ":")) for ch in chunks
    ]
    digest = hashlib.sha1()
    digest.update(raw_index.encode("utf-8"))
    for raw in raw_chunks:
        digest.update(raw.encode("utf-8"))
    version = digest.hexdigest()[:10]

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "d").mkdir(parents=True, exist_ok=True)
    clear_old_data_files(data_dir, version)
    index_name = "index-%s.json" % version
    (data_dir / index_name).write_text(raw_index, encoding="utf-8")
    chunk_names = []
    for i, raw in enumerate(raw_chunks):
        name = "%s-%s.json" % (version, i)
        (data_dir / "d" / name).write_text(raw, encoding="utf-8")
        chunk_names.append("data/d/" + name)

    manifest = {
        "v": version,
        "generatedAt": payload.get("generatedAt"),
        "count": payload.get("count", len(index_items)),
        "index": "data/" + index_name,
        "chunks": chunk_names,
        "thumbs": {
            "count": thumb_stats.get("count", 0),
            "clear": thumb_stats.get("clear", 0),
            "photo": thumb_stats.get("photo", 0),
            "bytes": thumb_stats.get("bytes", 0),
        },
    }
    (root / "data.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_marker(root / "index.html", version)

    index_bytes = len(raw_index.encode("utf-8"))
    details_bytes = sum(len(raw.encode("utf-8")) for raw in raw_chunks)
    print(
        "Index: %s (%s bytes, %s rijen)" % (index_name, index_bytes, len(index_items)),
        file=sys.stderr,
    )
    print(
        "Details: %s chunks, %s bytes totaal" % (DATA_CHUNKS, details_bytes),
        file=sys.stderr,
    )
    print("Versie: %s" % version, file=sys.stderr)
    return {
        "version": version,
        "indexBytes": index_bytes,
        "detailsBytes": details_bytes,
        "thumbs": manifest["thumbs"],
        "manifest": manifest,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--from-json", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--skip-thumbs", action="store_true")
    args = parser.parse_args()
    root = args.root or args.from_json.resolve().parent
    payload = json.loads(args.from_json.read_text(encoding="utf-8"))
    if "items" not in payload:
        raise SystemExit("Geen items in %s" % args.from_json)
    export_site(payload, root, args.cache, skip_thumbs=args.skip_thumbs)
