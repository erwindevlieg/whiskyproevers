#!/usr/bin/env python3
"""Verwerk zekere Drankdozijn-pagina's: letterlijke tekst, notities, vat en foto.

Alleen aanvullen; bestaande gegevens worden niet overschreven.
Prijzen worden niet opgeslagen. ABV-afwijking > 0,3 slaat de match over.
Pagina-extracten staan buiten de repo in /workspace/whisky_build/enrich/dd-pages/.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import enrich_sources as es

PAGES_DIR = Path("/workspace/whisky_build/enrich/dd-pages")
DD_IMAGES = Path("/workspace/whisky_build/ddcache/found-images.json")
ABV_TOLERANCE = 0.3
SOURCE_HOST = "drankdozijn.nl"


def abv_ok(item_abv, page_abv) -> bool:
    if item_abv in (None, "") or page_abv in (None, ""):
        return True
    try:
        return abs(float(item_abv) - float(page_abv)) <= ABV_TOLERANCE
    except (TypeError, ValueError):
        return True


def field_empty(item: dict, field: str) -> bool:
    val = item.get(field)
    if val is None or val == "":
        return True
    if field == "proefnotities" and str(val).strip().lower() == "onbekend":
        return True
    return False


def has_image(item: dict, desc_entry: dict | None) -> bool:
    if desc_entry and str(desc_entry.get("image") or "").startswith("https://"):
        return True
    if str(item.get("afbeelding") or "").startswith("https://"):
        return True
    return False


def apply_all(repo: Path) -> dict:
    items_by_id = {it["id"]: it for it in es.load_items(repo)}
    desc_path = repo / "descriptions.json"
    notes_path = repo / "notes.json"
    facts_path = repo / "facts.json"
    descriptions = es.load_descriptions(desc_path)
    notes = es.load_descriptions(notes_path)
    facts = es.load_descriptions(facts_path) if facts_path.is_file() else {}
    found_images = {}
    if DD_IMAGES.is_file():
        raw = json.loads(DD_IMAGES.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            found_images = raw

    stats: Counter = Counter()
    report = {
        "added_desc": [],
        "added_notes": [],
        "added_facts": [],
        "added_images": [],
        "skipped": [],
    }

    pages = sorted(PAGES_DIR.glob("*.json"))
    stats["pages"] = len(pages)

    for path in pages:
        page = json.loads(path.read_text(encoding="utf-8"))
        item_id = page["id"]
        url = page["url"]
        item = items_by_id.get(item_id)
        if not item:
            stats["missing_item"] += 1
            report["skipped"].append({"id": item_id, "reason": "item ontbreekt", "url": url})
            continue
        if page.get("abv_mismatch") or not abv_ok(item.get("abv"), page.get("abv")):
            stats["abv_mismatch"] += 1
            report["skipped"].append({
                "id": item_id,
                "naam": item.get("naam"),
                "reason": "ABV komt niet overeen",
                "itemAbv": item.get("abv"),
                "pageAbv": page.get("abv"),
                "url": url,
            })
            continue
        if page.get("name") and not es.confident_match(
            item["naam"], page["name"], item.get("abv"), page.get("abv")
        ):
            stats["name_mismatch"] += 1
            report["skipped"].append({
                "id": item_id,
                "naam": item.get("naam"),
                "page": page.get("name"),
                "reason": "paginatitel geen zekere match",
                "url": url,
            })
            continue

        if page.get("description"):
            if es.maybe_add_description(descriptions, item_id, page["description"], url, stats):
                report["added_desc"].append({"id": item_id, "naam": item.get("naam"), "url": url})

        if page.get("notes"):
            if es.maybe_add_notes(notes, item, page["notes"], url, stats):
                report["added_notes"].append({"id": item_id, "naam": item.get("naam"), "url": url})

        vat = (page.get("vattype") or "").strip()
        if vat and field_empty(item, "vattype"):
            entry = facts.get(item_id) or {}
            if not entry.get("vattype"):
                entry["vattype"] = vat
                facts[item_id] = entry
                stats["facts_vat"] += 1
                report["added_facts"].append({"id": item_id, "field": "vattype", "value": vat})

        image = (page.get("image") or "").strip()
        if image.startswith("https://") and "cloudinary.com" in image:
            found_images[item_id] = {"image": image, "url": url, "source": "drankdozijn"}
            if not has_image(item, descriptions.get(item_id)):
                entry = descriptions.get(item_id) or {}
                entry["image"] = image
                entry["imageSource"] = "drankdozijn"
                entry["imagePage"] = url
                descriptions[item_id] = entry
                stats["images_added"] += 1
                report["added_images"].append({"id": item_id, "naam": item.get("naam"), "image": image})
            else:
                stats["images_already"] += 1

    es.save_json(desc_path, descriptions)
    es.save_json(notes_path, notes)
    if facts:
        es.save_json(facts_path, facts)
    DD_IMAGES.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(DD_IMAGES, found_images)

    report["stats"] = dict(stats)
    return report


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    report = apply_all(repo)
    out = Path("/workspace/whisky_build/enrich/dd-apply-report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(out, report)
    s = report["stats"]
    print(
        "Pagina's: %s  omschrijvingen: %s  notities: %s  vat: %s  foto's: %s  "
        "al aanwezig desc/notes/foto: %s/%s/%s  overgeslagen ABV: %s  naam: %s"
        % (
            s.get("pages", 0),
            s.get("added", 0),
            s.get("notes_added", 0),
            s.get("facts_vat", 0),
            s.get("images_added", 0),
            s.get("already_had", 0),
            s.get("notes_already", 0),
            s.get("images_already", 0),
            s.get("abv_mismatch", 0),
            s.get("name_mismatch", 0),
        )
    )
    print("Rapport:", out)


if __name__ == "__main__":
    sys.exit(main() or 0)
