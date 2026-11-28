#!/usr/bin/env python3
"""Waarom heeft een whisky geen omschrijving, proefnotities of afbeelding? Eén oorzaak per whisky.

Oorzaken (in deze volgorde toegekend)
  nieuwe_bron      een nieuwe bron (enrich_extra.py) heeft een strikte match: wordt nu gevuld
  bijna_match      er is een winkel-/bottelaarspagina die net niet door de strikte check komt
                   (subreden: ABV, editie, maat, leeftijd, ...)
  naamverschil     zelfde merk/leeftijd/ABV, maar de naam wijkt af (andere schrijfwijze, extra woorden)
  onafhankelijk    onafhankelijke botteling of single cask zonder enige kandidaat in de winkels
  alleen_sample    nergens als fles te koop (alleen Whiskysite-sample, of uitverkocht)
  overig           wel een fles of pagina, maar daar staat de informatie niet (of geen kandidaat)

Kandidaten komen uit de catalogi die al lokaal staan: Drankgigant (sitemap), Drankdozijn (sitemap),
de extra winkels en bottelaarsites van enrich_extra.py (met pagina-check als de pagina gecachet is)
en het Drankgigant-verwerkingsrapport (afgewezen pagina's). Er wordt niets opgehaald.

Gebruik: python scripts/analyse_gaten.py [--out /workspace/whisky_build/gaten/analyse.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import enrich_drankgigant as edg
import enrich_extra as ex
import enrich_sources as es
import fetch_flesprijzen as ff

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("/workspace/whisky_build/gaten/analyse.json")
SINGLE_CASK_RE = re.compile(r"(?i)\bcask\s*(?:no\.?|nr\.?|#)?\s*\d|\bsingle\s+cask\b|\bsingle\s+barrel\b|#\d")


def has_desc(i): return bool(i.get("omschrijving"))
def has_notes(i): return bool(i.get("proefnotities") or i.get("notities"))
def has_img(i): return str(i.get("afbeelding") or "").startswith("http")


def reason_kind(diffs: list[str]) -> str:
    kinds = []
    for d in diffs:
        if d.startswith("ABV onbekend"):
            kinds.append("ABV onbekend op pagina")
        elif d.startswith("ABV"):
            kinds.append("ABV")
        elif d.startswith("maat"):
            kinds.append("maat")
        elif d.startswith("editie"):
            kinds.append("editie")
        elif "noemt jaartal" in d:
            kinds.append("jaartal ontbreekt/extra")
        elif "leeftijd" in d:
            kinds.append("leeftijd ontbreekt/extra")
        elif d.startswith("vatnummer"):
            kinds.append("vatnummer ontbreekt")
        elif d.startswith("naam"):
            kinds.append("naam")
        else:
            kinds.append(d.split(" ")[0])
    return " + ".join(sorted(set(kinds)))


def name_level(item, rec, source):
    """Vergelijk op titel alleen (geen pagina): pseudo-info met ABV uit de titel."""
    info = {"url": rec["url"], "titel": rec["name"], "specs": {}}
    m = es.ABV_RE.search(rec["name"].replace(",", "."))
    if m:
        info["specs"]["abv"] = m.group(0)
    diffs = ex.differences(item, info, source)
    # Op titelniveau zeggen 'ABV onbekend' en 'maat' niets.
    return [d for d in diffs if not d.startswith("ABV onbekend") and not d.startswith("maat")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    repo = args.repo
    items = es.load_items(repo)
    gaps = [i for i in items if not (has_desc(i) and has_notes(i) and has_img(i))]

    # Catalogi op titel
    catalogs = {}
    dg_cat = ff.CACHE_DIR / "catalogus-drankgigant.json"
    if dg_cat.is_file():
        catalogs["drankgigant"] = json.loads(dg_cat.read_text(encoding="utf-8"))
    dd_cat = ff.CACHE_DIR / "catalogus-drankdozijn.json"
    if dd_cat.is_file():
        catalogs["drankdozijn"] = json.loads(dd_cat.read_text(encoding="utf-8"))
    idx = {k: ex.build_index([dict(r) for r in v]) for k, v in catalogs.items()}

    # Pagina-niveau: extra bronnen (gecachet) + Drankgigant-afwijzingen
    accepted, review, _ = ex.matches(repo)
    review_by_id = defaultdict(list)
    for r in review:
        review_by_id[r["id"]].append(r)
    if edg.REPORT.is_file():
        for r in json.loads(edg.REPORT.read_text(encoding="utf-8")).get("afgewezen", []):
            reden = r["reden"]
            diffs = ["maat"] if reden.startswith("flesmaat") else (["ABV"] if reden == "ABV verschilt" else [reden])
            review_by_id[r["id"]].append({"bron": "Drankgigant", "kandidaatUrl": r["url"], "kandidaatNaam": r.get("pagina"), "verschil": diffs})

    rows = []
    for it in gaps:
        iid = it["id"]
        missing = [f for f, ok in (("omschrijving", has_desc(it)), ("proefnotities", has_notes(it)), ("afbeelding", has_img(it))) if not ok]
        ib = ex.item_bottler(it)
        single = bool(SINGLE_CASK_RE.search(it["naam"]))
        has_bottle = bool(it.get("flesLink"))
        near = list(review_by_id.get(iid, []))
        strict_any = False
        for src_key, ix in idx.items():
            for sc, rec in ex.candidates(it, ix, src_key, top=2):
                diffs = name_level(it, rec, src_key)
                if not diffs:
                    strict_any = True
                elif ex.near_match(diffs):
                    near.append({"bron": src_key, "kandidaatUrl": rec["url"], "kandidaatNaam": rec["name"], "verschil": diffs})
        nonname = [n for n in near if reason_kind(n["verschil"]) not in ("naam",)]
        if iid in accepted:
            cause = "nieuwe_bron"
            sub = "strikte match in: " + ", ".join(sorted({h[0] for h in accepted[iid]}))
        elif nonname:
            cause = "bijna_match"
            sub = reason_kind(nonname[0]["verschil"])
        elif near:
            cause, sub = "naamverschil", "naam"
        elif (ib or single) and not has_bottle and not strict_any:
            cause, sub = "onafhankelijk", ("bottelaar: %s" % ib if ib else "single cask")
        elif not has_bottle and not strict_any:
            note = (it.get("opmerkingFles") or "").lower()
            cause = "alleen_sample"
            sub = "uitverkocht/niet op voorraad" if re.search(r"niet op voorraad|uitverkocht|niet (meer )?leverbaar|niet te koop", note) else "geen fles in NL-winkels gevonden"
        else:
            cause = "overig"
            sub = "fles/pagina gevonden, maar daar staat dit veld niet" if (has_bottle or strict_any) else "geen kandidaat"
        rows.append({
            "id": iid, "naam": it["naam"], "ontbreekt": missing, "oorzaak": cause, "detail": sub,
            "bottelaar": ib, "singleCask": single, "flesGevonden": has_bottle or strict_any,
            "kandidaten": near[:3],
        })

    table = defaultdict(Counter)
    for r in rows:
        t = table[r["oorzaak"]]
        t["whiskys"] += 1
        for f in r["ontbreekt"]:
            t[f] += 1
    sub = defaultdict(Counter)
    for r in rows:
        if r["oorzaak"] == "onafhankelijk":
            sub[r["oorzaak"]][r["detail"].split(":")[0]] += 1
        elif r["oorzaak"] == "bijna_match":
            for part in r["detail"].split(" + "):
                sub[r["oorzaak"]][part] += 1
        else:
            sub[r["oorzaak"]][r["detail"]] += 1
    result = {
        "totaal": len(items),
        "metGat": len(gaps),
        "tabel": {k: dict(v) for k, v in table.items()},
        "subredenen": {k: dict(v.most_common()) for k, v in sub.items()},
        "whiskys": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("%-14s %8s %13s %14s %11s" % ("oorzaak", "whisky's", "omschrijving", "proefnotities", "afbeelding"))
    for k in ("nieuwe_bron", "bijna_match", "naamverschil", "onafhankelijk", "alleen_sample", "overig"):
        t = table.get(k, Counter())
        print("%-14s %8s %13s %14s %11s" % (k, t["whiskys"], t["omschrijving"], t["proefnotities"], t["afbeelding"]))
    for k, v in result["subredenen"].items():
        print(k, json.dumps(v, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
