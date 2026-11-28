#!/usr/bin/env python3
"""Bronnen-cascade: per veld staat vast waar de informatie vandaan komt.

Dit bestand is de enige plek met de bronvolgorde. De enrichers
(`enrich_dd.py`, `enrich_gall.py`, `enrich_drankgigant.py`, de Master of
Malt-verwerking in `enrich_sources.py`) en de runner `enrich_cascade.py`
lezen de volgorde hier; niets anders bepaalt welke bron voorgaat.

Regels
- Een veld met modus "eerste": de eerste bron in de volgorde die het veld heeft,
  wint. Een bron lager in de lijst vult alleen een LEEG veld. Een bestaande
  waarde wordt nooit overschreven.
- Een veld met modus "samenvoegen" (proefnotities, tags): alle bronnen mogen
  altijd bijdragen. Teksten worden ontdubbeld en elk stuk bewaart zijn eigen
  bron.
- Master of Malt: alleen omschrijving en proefnotities, nooit prijs of koop-link.
- Whiskybase wordt niet gescrapet (Cloudflare, niet omzeilen); de score en
  link komen uit het bestaande overzicht.
- Teksten worden letterlijk overgenomen. `format_description` verandert alleen
  witruimte en alinea-indeling, nooit woorden.
"""
from __future__ import annotations

import html as _html
import re

# Bronsleutel -> weergavenaam en hostnamen.
SOURCES = {
    "whiskysite": {"label": "Whiskysite", "hosts": ["whiskysite.nl"]},
    "maker": {"label": "Website van de maker", "hosts": []},
    "drankdozijn": {"label": "Drankdozijn", "hosts": ["drankdozijn.nl"]},
    "gall": {"label": "Gall & Gall", "hosts": ["gall.nl"]},
    "drankgigant": {"label": "Drankgigant", "hosts": ["drankgigant.nl"]},
    "masterofmalt": {"label": "Master of Malt", "hosts": ["masterofmalt.com"]},
    "whiskybase": {"label": "Whiskybase", "hosts": ["whiskybase.com"]},
}

# Per veld: modus en bronvolgorde (hoogste prioriteit eerst).
FIELDS = {
    "omschrijving": {
        "modus": "eerste",
        "volgorde": ["whiskysite", "maker", "drankdozijn", "gall", "drankgigant", "masterofmalt"],
    },
    "afbeelding": {
        "modus": "eerste",
        "volgorde": ["whiskysite", "maker", "drankdozijn", "drankgigant", "gall"],
    },
    "feiten": {  # regio, vattype, leeftijd, abv, inhoud: alleen lege velden
        "modus": "eerste",
        "volgorde": ["whiskysite", "drankdozijn", "gall", "drankgigant"],
    },
    "proefnotities": {
        "modus": "samenvoegen",
        "volgorde": ["whiskysite", "maker", "drankdozijn", "gall", "drankgigant", "masterofmalt"],
    },
    "tags": {  # smaaktags en vat/stijl-tags uit alle teksten
        "modus": "samenvoegen",
        "volgorde": ["whiskysite", "maker", "drankdozijn", "gall", "drankgigant", "masterofmalt"],
    },
    "flesprijs": {
        "modus": "laagste",
        "volgorde": ["drankgigant", "drankdozijn", "whiskysite"],
    },
}

# Wat een bron nooit mag leveren, wat er ook in de volgorde staat.
VERBODEN = {
    "masterofmalt": {"flesprijs", "koop-link"},
    "whiskybase": {"scrapen"},
}

# De stages van de runner draaien in de volgorde van de omschrijving.
STAGE_VOLGORDE = FIELDS["omschrijving"]["volgorde"]


def may_fill(source: str, field: str) -> bool:
    if field in VERBODEN.get(source, ()):
        return False
    return source in FIELDS[field]["volgorde"]


def rank(source: str, field: str) -> int:
    """Lager = hogere prioriteit. Onbekende bron: laatste."""
    order = FIELDS[field]["volgorde"]
    return order.index(source) if source in order else len(order)


def source_key(url: str) -> str:
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url or "").split("/")[0].lower())
    for key, spec in SOURCES.items():
        if any(host == h or host.endswith("." + h) for h in spec["hosts"]):
            return key
    return "maker" if host else ""


def describe_cascade() -> str:
    lines = []
    for field, spec in FIELDS.items():
        names = " > ".join(SOURCES[s]["label"] for s in spec["volgorde"])
        lines.append("%-14s %-11s %s" % (field, spec["modus"], names))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Opmaak van omschrijvingen (alleen witruimte en alinea's, nooit woorden)
# --------------------------------------------------------------------------

SECTION_LABELS = (
    "Colour", "Color", "Kleur", "Nose", "Neus", "Geur", "Aroma", "Palate", "Palet",
    "Taste", "Smaak", "Flavour", "Flavor", "Finish", "Afdronk", "Comment", "Opmerking",
)
_LABEL_ALT = "|".join(SECTION_LABELS)
# Een sectielabel aan het begin van een regel: "Neus:" of "Nose: tekst".
SECTION_LINE_RE = re.compile(r"^(%s)\s*:\s*(.*)$" % _LABEL_ALT, re.I)
# Een sectielabel midden in een alinea: alleen herkennen na zinseinde of spatie, met hoofdletter.
_INLINE_SECTION_RE = re.compile(r"(?<=[.!?”\"')\d%%])\s+(?=(?:%s)\s*:\s)" % _LABEL_ALT)
_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_END = tuple(".!?:;…”\"')")


def format_description(text: str) -> str:
    """Normaliseer witruimte en alinea's; herken sectielabels (Neus, Smaak, Afdronk, ...).

    - HTML-resten en entiteiten weg, nbsp en zero-width tekens naar gewone spaties,
      dubbele spaties weg.
    - Bron-alinea's (lege regels) blijven staan. Een enkele regelovergang midden in
      een zin (regel eindigt zonder leesteken, volgende begint met kleine letter)
      wordt een spatie. Andere enkele regelovergangen worden alinea-einden.
    - Een label als "Neus:" op een eigen regel wordt samengevoegd met de tekst
      eronder ("Neus: tekst"); een label midden in een alinea begint een nieuwe alinea.
    De woorden zelf blijven ongewijzigd.
    """
    if not text:
        return ""
    t = _html.unescape(str(text))
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = _TAG_RE.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[\u00a0\u2007\u202f]", " ", t)
    t = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", t)
    t = re.sub(r"[ \t\f\v]+", " ", t)
    lines = [ln.strip() for ln in t.split("\n")]
    # Sectielabel op eigen regel + tekst op de volgende regel.
    merged: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = SECTION_LINE_RE.match(ln)
        if m and not m.group(2) and i + 1 < len(lines) and lines[i + 1]:
            merged.append("%s: %s" % (m.group(1), lines[i + 1]))
            i += 2
            continue
        merged.append(ln)
        i += 1
    lines = merged
    # Regel midden in een zin samenvoegen.
    paras: list[str] = []
    cur = ""
    for ln in lines:
        if not ln:
            if cur:
                paras.append(cur)
                cur = ""
            paras.append("")
            continue
        if not cur:
            cur = ln
            continue
        starts_section = bool(SECTION_LINE_RE.match(ln))
        if not cur.endswith(_SENTENCE_END) and ln[:1].islower() and not starts_section:
            cur = cur + " " + ln
        else:
            paras.append(cur)
            cur = ln
    if cur:
        paras.append(cur)
    # Secties midden in een alinea afsplitsen.
    out: list[str] = []
    for p in paras:
        if not p:
            out.append("")
            continue
        for piece in _INLINE_SECTION_RE.split(p):
            piece = piece.strip()
            if piece:
                out.append(piece)
    # Alle niet-lege alinea's scheiden met één lege regel.
    blocks = [p for p in out if p]
    return "\n\n".join(blocks).strip()


def split_sections(text: str) -> list[dict]:
    """Splits een geformatteerde omschrijving in alinea's: {label|None, tekst}."""
    parts = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if not para:
            continue
        m = SECTION_LINE_RE.match(para)
        if m and m.group(2):
            parts.append({"label": m.group(1), "tekst": m.group(2).strip()})
        else:
            parts.append({"label": None, "tekst": para})
    return parts


NOTE_LABELS = {"nose", "neus", "geur", "aroma", "palate", "palet", "taste", "smaak", "flavour", "flavor", "finish", "afdronk"}


def explicit_note_sections(text: str) -> str:
    """Neus/Smaak/Afdronk-alinea's uit een omschrijving, letterlijk, als ze er expliciet staan."""
    kept = []
    for part in split_sections(text):
        if part["label"] and part["label"].lower() in NOTE_LABELS:
            kept.append("%s: %s" % (part["label"], part["tekst"]))
    return "\n".join(kept)


# --------------------------------------------------------------------------
# Vat- en stijltags uit tekst (alleen als het er expliciet staat)
# --------------------------------------------------------------------------

_CASK_WORD = (
    r"(?:cask(?!\s+strength)|casks|butt|butts|barrel|barrels|hogshead|hogsheads|puncheon|puncheons|pipe|pipes|"
    r"vat|vaten|fust|fusten|wood|oak|hout|eik|eiken|eikenhout|finish|finished|matured|maturation|"
    r"gerijpt|rijping|afwerking|gefinisht|aged|seasoned|\bex-)"
)
_NEG = r"(?:geen|niet|no|not|without|zonder|non|ongesherryd)[\s-]*$"


def _near(term: str, window: int = 28) -> list[re.Pattern]:
    """term vlak bij een vatwoord (ervoor of erna); groep 't' is de term zelf."""
    return [
        re.compile(r"(?P<t>%s)[\w\s,'’-]{0,%d}?%s" % (term, window, _CASK_WORD), re.I),
        re.compile(r"%s[\w\s,'’-]{0,%d}?(?P<t>%s)" % (_CASK_WORD, window, term), re.I),
    ]


# label -> (patroon, uitsluiting op voorgaande tekst)
CASK_TAG_PATTERNS = [
    ("Oloroso", [re.compile(r"(?P<t>\boloroso\b)", re.I)]),
    ("PX", [re.compile(r"(?P<t>\bpedro\s+xim[eé]nez\b|\bPX\b(?!\w))")]),
    ("Sherry", _near(r"sherry|sherries|amontillado|\bfino\b|manzanilla|jerez")),
    ("Bourbon", _near(r"bourbon")),
    ("Port", _near(r"\bport(?!\s+(?:ellen|charlotte|askaig|dundas|mor|wemyss|of\b|city))(?:wijn)?\b|portwood|\bruby\s+port|\btawny")),
    ("Rum", _near(r"\brum\b|\brhum\b")),
    ("Wijn", _near(r"red\s+wine|white\s+wine|rode\s+wijn|witte\s+wijn|\bwijn\b|\bwine\b|sauternes|barolo|bordeaux|burgundy|bourgogne|madeira|marsala|tokaji|rioja|syrah|pineau|\bchardonnay\b")),
    ("Cognac/Armagnac", _near(r"cognac|armagnac")),
    ("Virgin oak", [re.compile(r"(?P<t>virgin\s+(?:american\s+|french\s+|european\s+)?oak|nieuw(?:e)?\s+(?:amerikaans(?:e)?\s+|franse\s+)?eik(?:en|enhout)?|new\s+(?:american\s+|french\s+)?oak)", re.I)]),
]
# Zinnen die duiden op iets anders dan het gebruikte vat.
_CASK_NEG_BEFORE = re.compile(_NEG, re.I)


def cask_tags_from_text(text: str) -> list[str]:
    """Vat/stijl-tags die expliciet in de tekst staan (Oloroso, PX, Sherry, Bourbon, Port, Rum, Wijn, ...).

    Een vatwoord moet vlakbij de term staan ("sherry cask", "gerijpt op bourbonvaten");
    een negatie ("geen sherry", "niet-sherry") sluit de treffer uit.
    """
    if not text:
        return []
    t = str(text)
    found: list[str] = []
    for label, pats in CASK_TAG_PATTERNS:
        hit = False
        for pat in pats:
            for m in pat.finditer(t):
                before = t[max(0, m.start("t") - 14):m.start("t")]
                if _CASK_NEG_BEFORE.search(before):
                    continue
                hit = True
                break
            if hit:
                break
        if hit:
            found.append(label)
    return found


def clean_inline(text: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(text or "")).strip()
