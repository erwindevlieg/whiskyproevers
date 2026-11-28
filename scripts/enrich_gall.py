#!/usr/bin/env python3
"""Vul ontbrekende omschrijvingen, proefnotities en feiten aan uit Gall & Gall.

Alleen zekere matches (zelfde distilleerderij, leeftijd, editie en vat).
Bestaande echte gegevens worden niet overschreven. Geen prijzen of aankooplinks.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import enrich_sources as es

GALL_CACHE = Path("/workspace/whisky_build/gallcache")
PAGES = GALL_CACHE / "pages"
FACTS_FIELDS = ("leeftijd", "abv", "vattype", "regio", "inhoud", "flesGrootte")

INHOUD_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(centiliter|cl|ml|liter|ltr|l)\b", re.I)
AGE_JAAR_RE = re.compile(r"(?i)\b(\d{1,2})\s*(?:jaar(?:\s*oud)?|years?\s*old|years?\b|yo)\b")


def page_path(url: str) -> Path:
    leaf = urlparse(unquote(url)).path.rsplit("/", 1)[-1]
    return PAGES / (leaf or "page.html")


def parse_inhoud(value: str) -> str | None:
    if not value:
        return None
    m = INHOUD_RE.search(value.replace(",", "."))
    if not m:
        return None
    num = m.group(1).replace(".", ",") if "." in m.group(1) and not m.group(1).endswith(".0") else m.group(1).replace(".0", "")
    unit = m.group(2).lower()
    if unit in {"centiliter", "cl"}:
        unit = "cl"
    elif unit == "ml":
        unit = "ml"
    else:
        unit = "l"
    # normalize 70.0
    if num.endswith(",0"):
        num = num[:-2]
    return f"{num} {unit}"



def strip_promo_lines(text: str) -> str:
    if not text:
        return ""
    promo = re.compile(
        r"(?i)maak kans|maak jouw|exclusieve \w+ glazen|glazenset|kassabon|"
        r"premium member|copita glazen|ter waarde van\s*€|vul na je aankoop|"
        r"deelnameformulier|^lees meer$|^maar\s+\d+\s+flessen beschikbaar"
    )
    kept = []
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line or promo.search(line):
            continue
        line = re.sub(r"(?i)\s*op\s*=\s*op!?\s*", " ", line).strip()
        if line:
            kept.append(line)
    out = "\n".join(kept).strip()
    if out and re.search(r"[A-Za-zÀ-ÿ0-9'’)]$", out):
        out += "."
    return out

def parse_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    name = ""
    description = ""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        products = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            products = data["@graph"]
        for prod in products:
            if not isinstance(prod, dict):
                continue
            typ = prod.get("@type")
            if typ != "Product" and typ != ["Product"]:
                continue
            name = (prod.get("name") or name or "").strip()
            description = (prod.get("description") or description or "").strip()
            break
    if not name and soup.find("h1"):
        name = soup.find("h1").get_text(" ", strip=True)
    html_desc = ""
    node = soup.select_one(".pdp-info_description")
    if node:
        html_desc = es.clean_text(node.get_text("\n", strip=True))
        html_desc = re.sub(
            r"(?i)^let op[,:]? dit is een artikel met beperkte oplage(?:\s*op\s*=\s*op)?\s*",
            "",
            html_desc,
        ).strip()
        html_desc = es.strip_boilerplate(html_desc)
    if html_desc and len(html_desc) > len(description or ""):
        description = html_desc
    description = strip_promo_lines(description)
    info = {}
    for table in soup.select(".product-label__content table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) >= 2:
                info[cells[0].strip().lower()] = cells[1].strip()
    smaak = ""
    for h in soup.find_all("h2"):
        if h.get_text(" ", strip=True).lower() == "smaak":
            node = h.find_next(class_="pdp-details__item-label-secondary")
            if node:
                smaak = es.clean_text(node.get_text(" ", strip=True))
                smaak = re.sub(r"\s+,", ",", smaak)
                smaak = re.sub(r"\s+", " ", smaak).strip(" ,")
            break
    abv = es.parse_abv(info.get("alcoholpercentage") or "")
    inhoud = parse_inhoud(info.get("inhoud") or "")
    oorsprong = (info.get("oorsprong") or "").strip()
    if not description:
        description = info.get("description") or ""
    description = es.strip_boilerplate(description)
    # strip CTA leftovers
    description = re.sub(r"(?i)^let op[,:]? dit is een artikel met beperkte oplage.*?(?:\n|$)", "", description).strip()
    age = None
    m = AGE_JAAR_RE.search(name or "")
    if m:
        age = int(m.group(1))
    return {
        "url": url,
        "name": name,
        "description": description,
        "smaak": smaak,
        "abv": abv,
        "inhoud": inhoud,
        "oorsprong": oorsprong,
        "age": age,
        "info": info,
    }


def notes_from_page(page: dict) -> str:
    parts = []
    if page.get("smaak") and len(page["smaak"]) >= 20:
        parts.append(page["smaak"])
    # tastingsentences in the description
    text = page.get("description") or ""
    bits = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if re.search(r"(?i)\b(neus|geur|aroma|smaak|proeft|afdronk|tonen van|hints van)\b", sent):
            bits.append(sent.strip())
    if bits:
        parts.append(" ".join(bits))
    return es.clean_text("\n".join(dict.fromkeys(parts)))


def field_empty(item: dict, field: str) -> bool:
    val = item.get(field)
    if val is None or val == "":
        return True
    if field == "proefnotities" and str(val).strip().lower() == "onbekend":
        return True
    return False


def apply_all(repo: Path, write_facts: bool = True) -> dict:
    matches = json.loads((GALL_CACHE / "matches.json").read_text(encoding="utf-8"))
    items_by_id = {it["id"]: it for it in es.load_items(repo)}
    desc_path = repo / "descriptions.json"
    notes_path = repo / "notes.json"
    facts_path = repo / "facts.json"
    descriptions = es.load_descriptions(desc_path)
    notes = es.load_descriptions(notes_path)
    facts = es.load_descriptions(facts_path) if facts_path.is_file() else {}
    stats: Counter = Counter()
    report = {"added_desc": [], "added_notes": [], "added_facts": [], "skipped": []}

    for rec in matches:
        item = items_by_id.get(rec["id"])
        if not item:
            stats["missing_item"] += 1
            continue
        path = page_path(rec["url"])
        if not path.is_file():
            stats["missing_page"] += 1
            report["skipped"].append({"id": rec["id"], "reason": "geen pagina", "url": rec["url"]})
            continue
        page = parse_page(path.read_text(encoding="utf-8", errors="replace"), rec["url"])
        if not page["name"]:
            stats["no_name"] += 1
            continue
        # verifieer match tegen de paginatitel, tenzij handmatig gecontroleerd
        if not rec.get("manual"):
            if not es.confident_match(item["naam"], page["name"], item.get("abv"), page.get("abv")):
                stats["page_mismatch"] += 1
                report["skipped"].append({
                    "id": rec["id"], "naam": item["naam"], "page": page["name"],
                    "reason": "paginatitel geen zekere match", "url": rec["url"],
                })
                continue
        else:
            # Glenfiddich 30 Suspended Time / Time Series: leeftijd, ABV en 'tijd' moeten kloppen
            if page.get("age") not in (None, item.get("leeftijd")):
                stats["page_mismatch"] += 1
                continue
            if page.get("abv") and item.get("abv") and abs(page["abv"] - float(item["abv"])) > 0.3:
                stats["page_mismatch"] += 1
                continue
            blob = (page["name"] + " " + (page["description"] or "")).lower()
            if "tijd" not in blob and "suspended" not in blob and "time" not in blob:
                stats["page_mismatch"] += 1
                continue

        # ABV-controle ook bij confident_match (voor de zekerheid, o.a. Bushmills 21 40/46)
        if item.get("abv") is not None and page.get("abv") is not None:
            if abs(float(item["abv"]) - page["abv"]) > 0.3:
                stats["abv_mismatch"] += 1
                report["skipped"].append({
                    "id": rec["id"], "naam": item["naam"], "item_abv": item["abv"],
                    "page_abv": page["abv"], "reason": "ABV wijkt af", "url": rec["url"],
                })
                continue
        if item.get("leeftijd") is not None and page.get("age") is not None:
            if int(item["leeftijd"]) != int(page["age"]):
                stats["age_mismatch"] += 1
                report["skipped"].append({
                    "id": rec["id"], "naam": item["naam"], "reason": "leeftijd wijkt af",
                    "url": rec["url"],
                })
                continue

        url = rec["url"]
        # omschrijving: alleen als item én descriptions.json geen bruikbare tekst hebben
        existing_desc = (item.get("omschrijving") or (descriptions.get(item["id"]) or {}).get("text") or "").strip()
        if page["description"]:
            prev = descriptions.get(item["id"]) or {}
            same_src = (prev.get("sourceUrl") or "") == url
            if not es.is_useful_description(existing_desc):
                if es.maybe_add_description(descriptions, item["id"], page["description"], url, stats, item.get("naam")):
                    report["added_desc"].append({"id": rec["id"], "naam": item["naam"], "url": url})
            elif same_src and len(page["description"]) > len(existing_desc) + 40:
                prev = dict(prev)
                prev["text"] = es.strip_boilerplate(page["description"])
                prev["sourceUrl"] = url
                descriptions[item["id"]] = prev
                stats["desc_extended"] += 1

        # proefnotities
        note = notes_from_page(page)
        if note and es.maybe_add_notes(notes, item, note, url, stats):
            report["added_notes"].append({"id": rec["id"], "naam": item["naam"], "url": url})

        # feiten: alleen lege velden, nooit inhoud als flesGrootte al bekend is
        added_fields = {}
        entry = dict(facts.get(item["id"]) or {})
        if field_empty(item, "leeftijd") and page.get("age") is not None:
            entry["leeftijd"] = page["age"]
            added_fields["leeftijd"] = page["age"]
        if field_empty(item, "abv") and page.get("abv") is not None:
            entry["abv"] = page["abv"]
            added_fields["abv"] = page["abv"]
        if field_empty(item, "inhoud") and field_empty(item, "flesGrootte") and page.get("inhoud"):
            entry["inhoud"] = page["inhoud"]
            added_fields["inhoud"] = page["inhoud"]
        if added_fields:
            entry["sourceUrl"] = url
            facts[item["id"]] = entry
            stats["facts_added"] += 1
            stats["fact_fields"] += len(added_fields)
            report["added_facts"].append({"id": rec["id"], "naam": item["naam"], "fields": added_fields, "url": url})

    es.save_json(desc_path, descriptions)
    es.save_json(notes_path, notes)
    if write_facts:
        es.save_json(facts_path, facts)
    (GALL_CACHE / "apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"stats": dict(stats), "report": report}


def main(argv=None) -> int:
    repo = ROOT
    result = apply_all(repo)
    stats = result["stats"]
    rep = result["report"]
    print("Nieuwe omschrijvingen: %s" % len(rep["added_desc"]))
    print("Nieuwe proefnotities: %s" % len(rep["added_notes"]))
    print("Flessen met aangevulde feiten: %s (velden: %s)" % (len(rep["added_facts"]), stats.get("fact_fields", 0)))
    print("Overgeslagen: %s" % len(rep["skipped"]))
    print("Statistieken:", stats)
    if rep["skipped"]:
        print("Voorbeelden overgeslagen:")
        for row in rep["skipped"][:12]:
            print("  ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
