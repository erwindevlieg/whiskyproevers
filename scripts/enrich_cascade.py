#!/usr/bin/env python3
"""Draai de bronnen-cascade: vul lege velden aan in de volgorde van `sources.py`.

Stappen (in de volgorde van de omschrijving-cascade)
  1. Whiskysite-sample: bestaande omschrijvingen blijven staan.
  1b. Whiskysite-fles (`enrich_wsfles.py`): sitemap-match op de volle fles; vult lege velden
     en vervangt een stub (of een lagere bron) door de flespagina-tekst.
  2. Drankdozijn, Gall & Gall: verwerk de gecachete pagina's (alleen lege velden,
     proefnotities worden samengevoegd; prijzen en feiten van die bronnen niet).
  3. Drankgigant: `enrich_drankgigant.py` (pagina's eerst ophalen met --fetch-drankgigant).
  3b. Extra bronnen (`enrich_extra.py`): bottelaarsites direct na de maker, extra NL-winkels
     (Dramshop, Whiskybase Shop, Drankenhandel Leiden) na Drankgigant. Bijna-matches gaan naar
     data/match-review.json; alleen regels met "goedgekeurd": true worden gebruikt.
  3c. Extra matchronde (`enrich_rematch.py`): bij gaten mét een strikte Whiskysite-flesmatch
     worden flesfeiten (naam, leeftijd, ABV, jaartal, bottelaar, vatnummer) als extra
     sleutels gebruikt tegen dezelfde winkels. Alleen strikte matches; review wordt niet
     goedgekeurd.
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
import enrich_extra
import enrich_wsfles
import enrich_rematch
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


def run_wsfles(repo: Path, stats: Counter) -> dict:
    """Whiskysite-flespagina, direct na de sample: alleen lege velden, stubs mogen wijken."""
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    facts = es.load_descriptions(repo / "facts.json")
    report = enrich_wsfles.apply(repo, descriptions, notes, facts)
    es.save_json(repo / "descriptions.json", descriptions)
    es.save_json(repo / "notes.json", notes)
    es.save_json(repo / "facts.json", facts)
    for k, v in report["stats"].items():
        stats["wsfles:" + k] += v
    enrich_wsfles.REPORT.parent.mkdir(parents=True, exist_ok=True)
    es.save_json(enrich_wsfles.REPORT, report)
    return report


def run_extra(repo: Path, key: str, stats: Counter) -> dict:
    """Extra winkel of bottelaarsite (enrich_extra.py): alleen lege velden, notities samenvoegen."""
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    facts = es.load_descriptions(repo / "facts.json")
    report = enrich_extra.apply(repo, descriptions, notes, facts, [key])
    es.save_json(repo / "descriptions.json", descriptions)
    es.save_json(repo / "notes.json", notes)
    es.save_json(repo / "facts.json", facts)
    for k, v in report["stats"].items():
        stats["extra:" + k] += v
    EXTRA_REVIEW.extend(report["review"])
    return report


EXTRA_REVIEW: list = []


def edg_review(repo: Path) -> list[dict]:
    """Drankgigant-pagina's die alleen op ABV (klein verschil) of flesmaat afvielen."""
    if not edg.REPORT.is_file():
        return []
    prev = enrich_extra.load_review(repo)
    items = {it["id"]: it for it in es.load_items(repo)}
    rows = []
    for r in json.loads(edg.REPORT.read_text(encoding="utf-8")).get("afgewezen", []):
        item = items.get(r["id"])
        path = edg.cache_file(r["url"])
        if not item or not path.is_file():
            continue
        info = json.loads(path.read_text(encoding="utf-8"))
        diffs = []
        if r["reden"].startswith("flesmaat"):
            diffs.append("maat %s vs %s" % (item.get("flesGrootte") or "70 cl", info.get("grootte")))
        elif r["reden"] == "ABV verschilt" and info.get("abv") is not None and item.get("abv") is not None:
            diffs.append("ABV %s vs %s" % (enrich_extra.fmt(item["abv"]), enrich_extra.fmt(info["abv"])))
        else:
            diffs.append(r["reden"])
        if not enrich_extra.near_match(diffs):
            continue
        rows.append({
            "id": r["id"], "whisky": item["naam"], "bron": "Drankgigant", "kandidaatNaam": info.get("naam"),
            "kandidaatUrl": r["url"], "verschil": diffs,
            "waarschijnlijkheid": enrich_extra.likelihood(0.9, diffs),
            "goedgekeurd": (prev.get((r["id"], r["url"])) or {}).get("goedgekeurd"),
        })
    return rows


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



def facts_from_stubs(repo: Path, descriptions: dict, items: list, stats: Counter) -> None:
    """Vul lege feiten (vattype) uit bewaarde Whiskysite-stubregels; bestaande waarden blijven."""
    items_by_id = {it["id"]: it for it in items}
    facts = es.load_descriptions(repo / "facts.json")
    changed = False
    for iid, entry in descriptions.items():
        stub = (entry.get("stubText") or "").strip()
        if not stub:
            continue
        item = items_by_id.get(iid) or {}
        vat_lines = [ln for ln in stub.split("\n") if src.stub_line_kind(ln) == "Vat"]
        vat_empty = not item.get("vattype") or str(item.get("vattype")).lower().startswith("onbekend")
        fact = dict(facts.get(iid) or {})
        if vat_lines and vat_empty and not fact.get("vattype"):
            fact["vattype"] = "\n".join(vat_lines)
            if not fact.get("sourceUrl"):
                fact["sourceUrl"] = entry.get("stubSourceUrl") or ""
            facts[iid] = fact
            stats["stub_vattype"] += 1
            changed = True
    if changed:
        es.save_json(repo / "facts.json", facts)

def build_kenmerken(repo: Path, stats: Counter) -> dict:
    items = es.load_items(repo)
    items_by_id = {it["id"]: it for it in items}
    descriptions = es.load_descriptions(repo / "descriptions.json")
    notes = es.load_descriptions(repo / "notes.json")
    dd = dd_page_texts(items_by_id)
    dg = dg_page_texts(repo)
    extra = enrich_extra.page_texts(repo)
    wsfles = enrich_wsfles.page_texts(repo)
    result = {}
    for item in items:
        iid = item["id"]
        texts: list[tuple[str, str]] = [("naam", item.get("naam") or "")]
        vat = item.get("vattype")
        if vat and not str(vat).lower().startswith("onbekend"):
            texts.append(("vattype", str(vat)))
        entry = descriptions.get(iid) or {}
        if entry.get("stubText"):
            texts.append((entry.get("stubSourceUrl") or "whiskysite", entry["stubText"]))
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
        texts.extend(extra.get(iid, []))
        texts.extend(wsfles.get(iid, []))
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
    ap.add_argument("--fetch-extra", action="store_true", help="Haal eerst pagina's van de extra winkels en bottelaarsites op.")
    ap.add_argument("--fetch-wsfles", action="store_true", help="Haal eerst Whiskysite-flespagina's op (sitemap, crawl-delay 2 s).")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--skip-stages", default="", help="Komma-gescheiden bronsleutels om over te slaan.")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    skip = {s for s in args.skip_stages.split(",") if s}
    stats: Counter = Counter()
    print("Cascade:\n" + src.describe_cascade(), file=sys.stderr)

    if args.fetch_drankgigant:
        print("Drankgigant ophalen:", dict(edg.fetch_pages(repo, args.delay)), file=sys.stderr)
    if args.fetch_wsfles:
        print("Whiskysite-flespagina's ophalen:", dict(enrich_wsfles.fetch_pages(repo)), file=sys.stderr)
    if args.fetch_extra:
        print("Extra bronnen ophalen:", dict(enrich_extra.fetch(repo, enrich_extra.ALL_SOURCES)), file=sys.stderr)
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
        elif key == "whiskysitefles":
            run_wsfles(repo, stats)
        elif key in enrich_extra.ALL_SOURCES:
            run_extra(repo, key, stats)
        elif key == "masterofmalt":
            run_stage_fetched(repo, stats)
        # whiskysite (sample): bestaande waarden blijven staan; flespagina's komen in whiskysitefles.
    if "rematch" not in skip:
        print("Extra matchronde (Whiskysite-flesfeiten als sleutels)…", file=sys.stderr)
        enrich_rematch.main(["--repo", str(repo), "--apply"])
    wsfles_rev = []
    if enrich_wsfles.REPORT.is_file():
        wsfles_rev = json.loads(enrich_wsfles.REPORT.read_text(encoding="utf-8")).get("review") or []
    if EXTRA_REVIEW or wsfles_rev or not skip.issuperset(enrich_extra.ALL_SOURCES):
        rows = sorted(EXTRA_REVIEW + edg_review(repo) + wsfles_rev, key=lambda r: -r["waarschijnlijkheid"])
        enrich_extra.write_review(repo, rows)
        print("Bijna-matches voor review: %s (data/match-review.json)" % len(rows), file=sys.stderr)
    format_all(repo, stats)
    facts_from_stubs(repo, es.load_descriptions(repo / "descriptions.json"), es.load_items(repo), stats)
    kenmerken = build_kenmerken(repo, stats)
    es.save_json(repo / "kenmerken.json", kenmerken)
    print("Kenmerken: %s whisky's" % len(kenmerken), file=sys.stderr)
    for k, v in sorted(stats.items()):
        print("  %s: %s" % (k, v), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
