#!/usr/bin/env python3
"""Kenmerken die letterlijk in naam, omschrijving, notities of winkelkenmerken staan.

Er wordt niets geraden: een kenmerk bestaat alleen als de tekst het zegt. Elk gevonden
kenmerk bewaart de bron (URL of 'naam'). Bestaande waarden worden nooit overschreven;
de merge in build_data.py vult alleen lege velden (ncf, kleurstof, rook).

Kenmerken
  vatsterkte, natuurlijke kleur, niet koudgefilterd, single cask (+ vatnummer),
  distillatie- en bottelingsjaar (leeftijd voor NAS alleen als beide jaren er staan),
  fenolgehalte (ppm), oplage, first fill/refill, vatsoort, finish of volledige rijping
  en de duur van de finish.
"""
from __future__ import annotations

import re

I = re.I

CASK_STRENGTH = re.compile(
    r"\bcask[\s-]*strength\b|\bvatsterkte\b|\bbarrel[\s-]*proof\b|\bnatuurlijke\s+vatsterkte\b|\bvat[\s-]*sterkte\b", I)
NON_CHILL = re.compile(
    r"\bnon[\s-]*chill[\s-]*filter(?:ed|ation)?\b|\bun[\s-]*chill[\s-]*filtered\b|\bnot\s+chill[\s-]*filtered\b"
    r"|\bniet[\s-]*koud(?:e)?[\s-]*gefilterd\b|\bniet[\s-]*gekoeld[\s-]*gefilterd\b|\bzonder\s+koud(?:e)?\s*filtr\w+|\bNCF\b", I)
NATURAL_COLOUR = re.compile(
    r"\bnatural(?:ly)?[\s-]+colou?r(?:ed|ing)?\b|\bnatuurlijke\s+kleur\b|\bno\s+(?:added\s+)?colou?r(?:ing)?\b"
    r"|\b(?:geen|zonder)\s+(?:toegevoegde\s+)?(?:kleurstof(?:fen)?|karamel(?:kleurstof)?)\b|\bun[\s-]*colou?red\b"
    r"|\bcolou?ring[\s-]*free\b|\bniet\s+bijgekleurd\b|\bongekleurd\b|\bwithout\s+(?:added\s+)?(?:colou?r(?:ing)?|caramel)\b"
    r"|\bno\s+(?:e150a?|caramel)\b|\bnaturally\s+coloured\b|\bbijgekleurd\s*:\s*nee\b", I)
SINGLE_CASK = re.compile(
    r"\bsingle[\s-]+(?:\w+[\s-]+){0,2}?(?:cask|barrel|butt|hogshead|puncheon|barrique)\b|\benkel(?:e)?[\s-]*vat\b|\bsingle[\s-]*vat\b|\b[eé]{1,2}n\s+(?:enkel\s+)?vat\b", I)
CASK_NUMBER = re.compile(
    r"\b(?:cask|vat|butt|hogshead|barrel|barrique|puncheon)[\s-]*(?:no\.?|nr\.?|number|nummer|#|ref\.?)\s*[:.]?\s*([A-Z]{0,3}\d[\w/-]*)", I)
DISTILLED = re.compile(
    r"\b(?:distilled|distillation(?:\s+date)?|gedistilleerd|gedestilleerd|destilleerd)\b\s*(?:in|on|op|:|-)?\s*"
    r"(?:(?:\d{1,2}[./-]){1,2}|[A-Za-z]+\s+)?((?:19|20)\d{2})\b", I)
BOTTLED = re.compile(
    r"\b(?:bottled|bottling\s+date|gebotteld|botteldatum)\b\s*(?:in|on|op|:|-)?\s*"
    r"(?:(?:\d{1,2}[./-]){1,2}|[A-Za-z]+\s+)?((?:19|20)\d{2})\b", I)
VINTAGE = re.compile(r"\bvintage\s+((?:19|20)\d{2})\b|\b((?:19|20)\d{2})\s+vintage\b", I)
PPM = re.compile(r"\b(\d+(?:[.,]\d+)?)(?:\s*(?:-|–|tot|to)\s*(\d+(?:[.,]\d+)?))?\s*ppm\b", I)
OPLAGE_A = re.compile(
    r"(?:limited\s+(?:edition\s+)?(?:to|of)|beperkt(?:e\s+oplage)?\s+(?:tot|van)|oplage(?:\s+van)?|outturn|"
    r"slechts|only|uitsluitend|exclusief)\s*:?\s*(?:ca\.?\s*)?(\d[\d.,]*)\s*(?:bottles|flessen|bouteilles|btls|stuks|flesjes)?", I)
OPLAGE_B = re.compile(r"\b(\d[\d.,]*)\s*(?:bottles|flessen|bouteilles)\b(?!\s*\d)", I)
OPLAGE_OF = re.compile(r"\b\d+\s+of\s+(\d[\d.,]*)\s*(?:bottles|flessen|bouteilles)\b", I)
OPLAGE_CTX = re.compile(r"limited|gelimiteerd|beperkt|oplage|outturn|released|uitgebracht|worldwide|wereldwijd|only|slechts|exclusive", I)
FIRST_FILL = re.compile(r"\bfirst[\s-]*fills?\b|\b1st[\s-]*fills?\b|\beerste[\s-]*vulling\b|\bfirst[\s-]*filled\b", I)
REFILL = re.compile(r"\bre[\s-]*fill(?:ed)?\b|\bsecond[\s-]*fill\b|\b2nd[\s-]*fill\b|\btweede[\s-]*vulling\b", I)
CASK_KINDS = [
    ("Hogshead", re.compile(r"\bhogsheads?\b", I)),
    ("Butt", re.compile(r"\bbutts?\b", I)),
    ("Barrique", re.compile(r"\bbarriques?\b", I)),
    ("Puncheon", re.compile(r"\bpuncheons?\b", I)),
    ("Quarter cask", re.compile(r"\bquarter[\s-]*casks?\b", I)),
    ("Octave", re.compile(r"\boctaves?\b", I)),
    ("Pipe", re.compile(r"\bpipes?\b", I)),
    ("Barrel", re.compile(r"\bbarrels?\b(?![\s-]*(?:proof|strength))", I)),
]
FINISH = re.compile(
    r"\bfinish(?:ed)?\s+(?:in|on|for|with|op|met)\b|\b(?:cask|wood|barrel|butt|hogshead|vat|barrique|wine|wijn)[\s-]*finish\b"
    r"|\bgefinisht\b|\bafgewerkt\b|\bafwerking\s+(?:op|in|met)\b|\bdouble[\s-]*matured\b|\bextra[\s-]*matured\b"
    r"|\bsecondary\s+matur\w+|\bafwerking\b(?!\s*:)|\bfinishing\b", I)
FULL_MATURATION = re.compile(
    r"\bfully\s+matured\b|\bfull\s+maturation\b|\bvolledig\s+gerijpt\b|\bgeheel\s+gerijpt\b|\bvolledige\s+rijping\b"
    r"|\b(?:entirely|completely|exclusively)\s+(?:matured|aged)\b|\bmatured\s+entirely\b|\bfully[\s-]aged\b", I)
UNIT_NL = {"month": "maanden", "months": "maanden", "maand": "maand", "maanden": "maanden", "year": "jaar", "years": "jaar",
           "jaar": "jaar", "week": "weken", "weeks": "weken", "weken": "weken", "day": "dagen", "days": "dagen", "dagen": "dagen"}
FINISH_DURATION = re.compile(
    r"(?:finish\w*|afwerking|afgewerkt|gefinisht|extra[\s-]*matur\w+|double[\s-]*matur\w+|matured\s+(?:further|additionally))"
    r"[^.;\n]{0,70}?\b(\d+(?:[.,]\d+)?)\s*[- ]?(months?|maanden|maand|years?|jaar|weeks?|weken|days?|dagen)\b", I)
FINISH_DURATION_REV = re.compile(
    r"\b(\d+(?:[.,]\d+)?)[\s-]*(months?|maanden|maand|weeks?|weken)[\s-]*(?:finish|afwerking|finishing|extra\s+rijping)", I)

FIELDS = (
    "vatsterkte", "natuurlijkeKleur", "nietKoudgefilterd", "singleCask", "vatNummer",
    "distillatiejaar", "bottelingsjaar", "leeftijdAfgeleid", "ppm", "ppmWaarde", "oplage",
    "firstFill", "refill", "vatSoort", "finishType", "finishDuur",
)


def _num(text: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        return float(re.sub(r"[.,]", "", text))
    return float(text.replace(",", "."))


def extract(item: dict, texts: list[tuple[str, str]]) -> dict:
    """texts: [(bron, tekst)] in volgorde van prioriteit (naam eerst). Geeft {veld: {w, b}}."""
    out: dict[str, dict] = {}

    def put(field, value, bron):
        if field not in out and value not in (None, "", False):
            out[field] = {"w": value, "b": bron}

    for bron, text in texts:
        if not text:
            continue
        t = str(text)
        if CASK_STRENGTH.search(t):
            put("vatsterkte", True, bron)
        if NON_CHILL.search(t):
            put("nietKoudgefilterd", True, bron)
        if NATURAL_COLOUR.search(t):
            put("natuurlijkeKleur", True, bron)
        if SINGLE_CASK.search(t):
            put("singleCask", True, bron)
        m = CASK_NUMBER.search(t)
        if m:
            put("vatNummer", m.group(1), bron)
        m = DISTILLED.search(t) or VINTAGE.search(t)
        if m:
            put("distillatiejaar", int(next(g for g in m.groups() if g)), bron)
        m = BOTTLED.search(t)
        if m:
            put("bottelingsjaar", int(m.group(1)), bron)
        m = PPM.search(t)
        if m:
            a, b = _num(m.group(1)), (_num(m.group(2)) if m.group(2) else None)
            if 0 < a < 300 and (b is None or a < b < 300):
                put("ppm", ("%g–%g" % (a, b)) if b else ("%g" % a), bron)
                put("ppmWaarde", round((a + b) / 2, 1) if b else a, bron)
        for m in OPLAGE_A.finditer(t):
            n = _num(m.group(1).rstrip(".,"))
            is_year = re.fullmatch(r"(?:19|20)\d\d", m.group(1)) is not None
            explicit_unit = re.search(r"(?:bottles|flessen|bouteilles|btls|stuks|flesjes)\s*$", m.group(0), I) is not None
            labelled = m.group(0).lower().startswith(("oplage", "outturn"))
            if 10 <= n <= 200000 and (explicit_unit or labelled) and not (is_year and not explicit_unit):
                put("oplage", int(n), bron)
                break
        else:
            m = OPLAGE_OF.search(t)
            if m and 10 <= _num(m.group(1).rstrip(".,")) <= 200000:
                put("oplage", int(_num(m.group(1).rstrip(".,"))), bron)
            for m in OPLAGE_B.finditer(t):
                ctx = t[max(0, m.start() - 60):m.end() + 40]
                n = _num(m.group(1).rstrip(".,"))
                if 20 <= n <= 200000 and OPLAGE_CTX.search(ctx):
                    put("oplage", int(n), bron)
                    break
        if FIRST_FILL.search(t):
            put("firstFill", True, bron)
        if REFILL.search(t):
            put("refill", True, bron)
        kinds = [label for label, pat in CASK_KINDS if pat.search(t)]
        if kinds:
            cur = out.get("vatSoort", {"w": [], "b": bron})
            for k in kinds:
                if k not in cur["w"]:
                    cur["w"].append(k)
            out["vatSoort"] = cur
        if FINISH.search(t):
            put("finishType", "finish", bron)
            age = item.get("leeftijd")
            for pat in (FINISH_DURATION, FINISH_DURATION_REV):
                for m in pat.finditer(t):
                    unit = UNIT_NL[m.group(2).lower()]
                    # Het getal naast 'jaar' kan de leeftijd van de whisky zijn, geen duur van de finish.
                    if unit == "jaar" and age not in (None, "", "onbekend") and str(age) == m.group(1):
                        continue
                    put("finishDuur", "%s %s" % (m.group(1).replace(".", ","), unit), bron)
                    break
                if "finishDuur" in out:
                    break
        elif FULL_MATURATION.search(t):
            put("finishType", "volledige rijping", bron)
    # NAS: leeftijd alleen uit twee expliciete jaren.
    d, b = out.get("distillatiejaar"), out.get("bottelingsjaar")
    if d and b and item.get("leeftijd") in (None, "", "onbekend") and 0 < b["w"] - d["w"] <= 80:
        out["leeftijdAfgeleid"] = {"w": b["w"] - d["w"], "b": b["b"] if b["b"] == d["b"] else d["b"]}
    return out
