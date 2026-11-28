#!/usr/bin/env python3
"""Draai de bronnen-cascade: vul lege velden aan in de volgorde van `sources.py`.

Stappen (in de volgorde van de omschrijving-cascade)
  1. Whiskysite / maker: bestaande omschrijvingen en foto's blijven staan.
  2. Drankdozijn, Gall & Gall: verwerk de gecachete pagina's (alleen lege velden,
     proefnotities worden samengevoegd; prijzen en feiten van die bronnen niet).
  3. Drankgigant: `enrich_drankgigant.py` (pagina's eerst ophalen met --fetch-drankgigant).
  4. Master of Malt en overige opgeslagen pagina's: alleen omschrijving en proefnotities.
  5. Opmaak van alle omschrijvingen en notities (witruimte en alinea's, geen woorden).
  6. Kenmerken uit alle teksten (`attributes.py`) naar kenmerken.json.

Daarna: python scripts/build_data.py <excel> (neemt notes.json, descriptions.json,
facts.json en kenmerken.json op).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import attributes
import enrich_dd
import enrich_drankgigant as edg
import enrich_gall
import enrich_sources as es
import sources as src

ROOT = Path(__file__).resolve().parent.parent
DD_PAGES = enrich_dd.PAGES_DIR


def run_stage_fetched(repo: Path, stats: Counter) -> None:
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    es.apply_fetched_markdown(es.load_items(repo), descriptions, notes, stats)
    es.save_json(repo / "descriptions.json", descriptions)
    es.save_json(repo / "notes.json", notes)


def run_drankgigant(repo: Path, stats: Counter) -> dict:
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    facts = es.load_descriptions(repo / "facts.json")
    report = edg.apply_cache(repo, descriptions, notes, facts)
    es.save_json(repo / "descriptions.json", descriptions)
    es.save_json(repo / "notes.json", notes)
    if facts:
        es.save_json(repo / "facts.json", facts)
    for k, v in report["stats"].items():
        stats["dg:" + k] += v
    edg.REPORT.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(edg.REPORT, report)
    return report


def format_all(repo: Path, stats: Counter) -> None:
    """Witruimte en alinea's; de woorden blijven zoals ze zijn."""
    descriptions = es.load_descriptions(repo / "descriptions.json")
    for entry in descriptions.values():
        if entry.get("text"):
            new = src.format_description(entry["text"])
            if new != entry["text"]:
                entry["text"] = new
                stats["opmaak_omschrijving"] += 1
    es.save_json(repo / "descriptions.json", descriptions)
    notes = es.load_descriptions(repo / "notes.json")
    for entry in notes.values():
        for rec in [entry] + list(entry.get("extra") or []):
            if rec.get("text"):
                new = src.format_description(rec["text"])
                if new != rec["text"]:
                    rec["text"] = new
                    stats["opmaak_notitie"] += 1
    es.save_json(repo / "notes.json", notes)


def dd_page_texts(items_by_id: dict) -> dict[str, list[tuple[str, str]]]:
    """Volledige Drankdozijn-teksten (alleen zekere matches), voor kenmerken."""
    out: dict[str, list[tuple[str, str]]] = {}
    if not DD_PAGES.is_dir():
        return out
    for path in sorted(DD_PAGES.glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        item = items_by_id.get(page.get("id"))
        if not item or not enrich_dd.abv_ok(item.get("abv"), page.get("abv")):
            continue
        if page.get("name") and not es.confident_match(item["naam"], page["name"], item.get("abv"), page.get("abv")):
            continue
        text = "\n".join(x for x in (page.get("description"), page.get("notes")) if x)
        if text:
            out.setdefault(item["id"], []).append((page["url"], text))
    return out


def dg_page_texts(repo: Path) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    by_url, _ = edg.candidate_pages(repo)
    for url, group in by_url.items():
        path = edg.cache_file(url)
        if not path.is_file():
            continue
        info = json.loads(path.read_text(encoding="utf-8"))
        kenm = info.get("kenmerken") or {}
        extra = "\n".join("%s: %s" % (k, kenm[k]) for k in ("Filtering", "Bijgekleurd", "Verkrijgbaarheid") if kenm.get(k))
        text = "\n".join(x for x in (info.get("omschrijving"), extra) if x)
        for item in group:
            ok, _why = edg.accept_info(item, info)
            if ok and text:
                out.setdefault(item["id"], []).append((url, text))
    return out


def build_kenmerken(repo: Path, stats: Counter) -> dict:
    items = es.load_items(repo)
    items_by_id = {it["id"]: it for it in items}
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    dd = dd_page_texts(items_by_id)
    dg = dg_page_texts(repo)
    result = {}
    for item in items:
        iid = item["id"]
        texts: list[tuple[str, str]] = [("naam", item.get("naam") or "")]
        vat = item.get("vattype")
        if vat and not str(vat).lower().startswith("onbekend"):
            texts.append(("vattype", str(vat)))
        entry = descriptions.get(iid) or {}
        if entry.get("text"):
            texts.append((entry.get("sourceUrl") or "omschrijving", entry["text"]))
        elif item.get("omschrijving"):
            texts.append((item.get("omschrijvingBron") or "omschrijving", item["omschrijving"]))
        nentry = notes.get(iid) or {}
        for rec in ([nentry] if nentry.get("text") else []) + list(nentry.get("extra") or []):
            texts.append((rec.get("sourceUrl") or "proefnotities", rec["text"]))
        if item.get("proefnotities") and str(item["proefnotities"]).lower() != "onbekend":
            texts.append((item.get("bronNotities") or "proefnotities", str(item["proefnotities"])))
        texts.extend(dd.get(iid, []))
        texts.extend(dg.get(iid, []))
        found = attributes.extract(item, texts)
        if found:
            result[iid] = found
            for field in found:
                stats["kenmerk:" + field] += 1
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--fetch-drankgigant", action="store_true", help="Haal eerst Drankgigant-pagina's op.")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--skip-stages", default="", help="Komma-gescheiden bronsleutels om over te slaan.")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    skip = {s for s in args.skip_stages.split(",") if s}
    stats: Counter = Counter()
    print("Cascade:\n" + src.describe_cascade(), file=sys.stderr)

    if args.fetch_drankgigant:
        print("Drankgigant ophalen:", dict(edg.fetch_pages(repo, args.delay)), file=sys.stderr)
    for key in src.STAGE_VOLGORDE:
        if key in skip:
            continue
        if key == "drankdozijn":
            rep = enrich_dd.apply_all(repo, write_facts=False)
            for k, v in rep["stats"].items():
                stats["dd:" + k] += v
        elif key == "gall":
            res = enrich_gall.apply_all(repo, write_facts=False)
            for k, v in res["stats"].items():
                stats["gall:" + k] += v
        elif key == "drankgigant":
            run_drankgigant(repo, stats)
        elif key == "masterofmalt":
            run_stage_fetched(repo, stats)
        # whiskysite en maker: bestaande waarden blijven staan.
    format_all(repo, stats)
    kenmerken = build_kenmerken(repo, stats)
    es.save_json(repo / "kenmerken.json", kenmerken)
    print("Kenmerken: %s whisky's" % len(kenmerken), file=sys.stderr)
    for k, v in sorted(stats.items()):
        print("  %s: %s" % (k, v), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
