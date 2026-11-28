# Whisky proefoverzicht

Statische pagina met samples van Whiskysite, scores van Whiskybase en flesprijzen in Nederland. Geschikt voor GitHub Pages: `index.html`, de compacte index in `data/`, `descriptions.json`, `assets/app.css` en kleine lijst-thumbs in `assets/thumbs/`.

Elke botteling heeft een eigen detailpagina op `#/whisky/<id>`, met alle gegevens en pas daar de links naar externe sites. De naam in het overzicht opent die pagina. Via **Vergelijk** (twee of drie flessen, ook te delen als `#/vergelijk/<id>,<id>`) zet je ze naast elkaar. `#/distilleerderijen` is het overzicht van distilleerderijen; elke stokerij heeft een pagina op `#/distilleerderij/<slug>` met cijfers, een grafiek en de bottelingen. `#/inzicht` toont een spreidingsdiagram van score versus sampleprijs, alleen voor rijen waarvan beide bekend zijn.

Het uiterlijk volgt de tokens en componentstijl van [shadcn/ui](https://ui.shadcn.com/) (zinc, radius 0,5rem). Licht, donker of systeem kies je zelf; de standaard volgt de instelling van het apparaat. Het overzicht heeft een compacte lijst en een kaartweergave, met zichtbare sorteerknoppen (WB-score, naam, leeftijd, sampleprijs, flesprijs, ABV). Filters, sortering, zoekterm en weergave staan in de URL (`#/?q=…&sort=score`) en worden ook in localStorage bewaard, zodat je een filterset kunt delen of later terugkrijgt. De CSS wordt lokaal gebouwd en meegecommitteerd; er is geen runtime-CDN voor styles.

```bash
npx tailwindcss@3 -c styles/tailwind.config.js -i styles/input.css -o assets/app.css --minify
```

## Data bijwerken

Het Excel-bestand blijft de bron. Persoonlijke kolommen (`Past bij smaak`, `Toelichting smaak`) worden niet geëxporteerd. Bij het genereren krijgt elke rij een stabiele `id` (URL-slug).

Als Whiskysite dezelfde whisky als 3 cl- en 6 cl-sample verkoopt, worden die rijen tot één botteling samengevoegd. Alleen zekere matches (zelfde naam zonder samplemaat, plus dezelfde ABV en leeftijd) gaan samen; twijfelgevallen blijven aparte rijen. De samengevoegde rij heeft een `samples`-lijst (maat, prijs, link). Oude id's blijven werken: de index bevat `redirects`, en de pagina stuurt `#/whisky/<oude-id>` door naar de nieuwe id. Geproefd- en vergelijkmarkeringen in localStorage worden meegenomen.

Productomschrijvingen van Whiskysite.nl en productfoto-URL’s haal je apart op (ruwe HTML wordt gecached, opnieuw draaien haalt alleen ontbrekende pagina’s op). Voor de lijst en kaarten maakt `build_data.py` kleine WebP-thumbs (circa 160 px hoog, met transparante achtergrond waar de rand egaal is) in `assets/thumbs/`. De grote flesfoto op de detailpagina blijft een hotlink naar het origineel.

```bash
python scripts/fetch_descriptions.py
python scripts/fetch_images.py
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
```

`build_data.py` schrijft een kleine `data.json` (manifest met versiehash), de compacte lijstindex `data/index-<hash>.json` (alleen velden voor lijst, filters, sortering en zoeken) en detailgegevens in `data/d/<hash>-<n>.json`, die de pagina pas ophaalt op een detail-, vergelijk- of andere pagina die ze nodig heeft. De versiehash staat ook in `index.html`, zodat browsers de bestanden goed kunnen cachen. Opnieuw publiceren zonder Excel: `python scripts/build_data.py --from-json .full-data.json`. (`.full-data.json` is een lokale kopie van de complete rijen en wordt niet gecommit.)

`fetch_descriptions.py` schrijft `descriptions.json`. `fetch_images.py` vult daarin `image` (hotlink) met deze volgorde: flespagina (Whiskysite of andere winkel), daarna een zekere Whiskysite-zoekmatch op de fles, daarna Drankdozijn (alleen zekere naam-match; live-pagina’s van Drankdozijn zijn vaak Cloudflare-geblokkeerd voor kale HTTP, daarom worden gerenderde/gecachte pagina’s gebruikt), daarna Whiskybase (alleen bij WB-status exact of labelversie; bij een Cloudflare-blokkade wordt live overgeslagen), en pas daarna een non-generieke samplefoto. Generieke 3/6 cl-sampleplaatjes worden overgeslagen. `build_data.py` voegt `omschrijving`, `omschrijvingBron` en `afbeelding` toe wanneer die er zijn. Omschrijvingen worden letterlijk overgenomen, niet herschreven. De pagina laadt `descriptions.json` op de detailweergave als fallback voor de tekst.

Het blok Omschrijving op de detailpagina verschijnt alleen als er een shoptekst is; andere ontbrekende velden blijven leeg of `onbekend`. Er wordt niets verzonnen.

## Flesprijzen

De flesprijs in de lijst en op de detailpagina is de laagste leverbare prijs over alle winkels. Naast de prijzen uit het Excel-bestand komen er prijzen uit `prijzen.json`, die `scripts/fetch_flesprijzen.py` schrijft vanuit de sitemaps en productpagina's van Drankgigant.nl en DrankDozijn. Er wordt alleen een prijs opgeslagen bij een zekere match (naam, leeftijd, editie, ABV binnen 0,3 en flesmaat 70 cl of de flesmaat van de whisky), en alleen voor leverbare flessen; samples, miniaturen en cadeausets tellen niet. Per prijs staan winkel, link, flesmaat en controledatum in `prijzen.json`. `build_data.py` voegt ze samen met de bestaande prijs: de laagste staat bovenaan, de volgende als tweede winkel en de rest in een lijst op de detailpagina. DrankDozijn toont soms een doorgestreepte oude prijs naast de actuele prijs; de actuele prijs (JSON-LD `offers.price`) telt.

```bash
python scripts/fetch_flesprijzen.py --shop drankgigant
python scripts/fetch_flesprijzen.py --shop drankdozijn --cdp-port 9333   # Chrome met --remote-debugging-port, Cloudflare blokkeert kale HTTP
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
```

Het script is bedoeld om later als GitHub Action te draaien; die staat nu niet aan. DrankDozijn vraagt daarvoor een echte browser (Cloudflare).


## Bronnen-cascade

`scripts/sources.py` legt per veld vast waar informatie vandaan komt. Een veld met modus *eerste* wordt alleen gevuld als het leeg is, in de volgorde van de lijst; bestaande waarden worden nooit overschreven. Proefnotities en tags worden uit alle bronnen samengevoegd, ontdubbeld, met de bron per stuk.

| veld | modus | volgorde |
| --- | --- | --- |
| omschrijving | eerste | Whiskysite > website van de maker > Drankdozijn > Gall & Gall > Drankgigant > Master of Malt |
| afbeelding | eerste | Whiskysite > maker > Drankdozijn > Drankgigant > Gall & Gall |
| feiten | eerste | Whiskysite > Drankdozijn > Gall & Gall > Drankgigant |
| proefnotities, tags | samenvoegen | alle bronnen |
| flesprijs | laagste | Drankgigant, Drankdozijn, Whiskysite |

Master of Malt levert nooit prijzen of koop-links. Whiskybase wordt niet gescrapet. Teksten worden letterlijk overgenomen; `format_description` past alleen witruimte en alinea's aan en herkent secties als Neus/Nose, Smaak/Palate en Afdronk/Finish.

```bash
python scripts/enrich_cascade.py --fetch-drankgigant   # alle stappen in cascade-volgorde
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
python scripts/check_tags.py                           # lijst en detail hebben dezelfde tags
```

`enrich_drankgigant.py` gebruikt dezelfde strikte match als de flesprijzen (naam, bottelaar, leeftijd, editie, ABV binnen 0,3, flesmaat; geen samples of cadeausets) en haalt omschrijving, proefnotities en foto op. `facts.json` bevat aangevulde feiten, `kenmerken.json` de kenmerken uit `scripts/attributes.py` (vatsterkte, natuurlijke kleur, niet koudgefilterd, single cask en vatnummer, distillatie- en bottelingsjaar, fenolgehalte, oplage, first fill/refill, vatsoort, finish en duur). Een kenmerk staat er alleen als de tekst het letterlijk zegt, met de bron erbij.

## Extra bronnen en bijna-matches

`scripts/enrich_extra.py` voegt informatiebronnen toe aan de cascade (geen prijzen):

| bron | soort | hoe |
| --- | --- | --- |
| Dramshop.nl, Whiskybase Shop (shop.whiskybase.com, de winkel, niet de database), Drankenhandel Leiden | NL-winkels (Lightspeed) | openbare collectielijst en `product.html?format=json`, crawl-delay 2 s volgens robots.txt |
| Gordon & MacPhail | bottelaar | productpagina's uit de sitemap (sterkte, jaartal, leeftijd, vat, proefnotities, foto) |
| Douglas Laing, North Star Spirits | bottelaar (Shopify) | `/products.json` |
| Cadenhead | bottelaar (WooCommerce) | productsitemap; bevat alleen de huidige uitgaven |

Bottelaarsites staan in de volgorde direct na de maker, de extra winkels na Drankgigant. Ze vullen alleen lege velden; proefnotities en tags worden samengevoegd met de bron per stuk. Whiskysite-feiten winnen bij tegenspraak.

De match is even streng als bij Drankgigant: zelfde bottelaar, `confident_match` op de titel aangevuld met de feiten van de pagina (ABV, vatnummer, leeftijd), ABV binnen 0,3, zelfde flesmaat, plus gelijke onderscheidende woorden. Bij een single cask volstaat in plaats van de leeftijd: hetzelfde vatnummer, hetzelfde distillatiejaar en ABV binnen 0,1. Noemt de pagina een leeftijd of jaartal dat niet in de naam staat, dan is het geen zekere match.

Wat net niet door die check komt (een of twee zachte verschillen: leeftijd of jaartal ontbreekt, editiewoord, ABV niet op de pagina of tot 1 % anders, andere flesmaat, andere schrijfwijze) komt in `data/match-review.json`, met whisky, kandidaat-URL, bron en verschil. Zet `"goedgekeurd": true` om een koppeling te gebruiken (ook voor Drankgigant-pagina's die op ABV of maat afvielen), `false` om hem te verwerpen. De cascade gebruikt alleen `true`; bij een nieuwe run blijven die keuzes staan.

`scripts/analyse_gaten.py` deelt elke whisky zonder omschrijving, proefnotities of foto in naar één oorzaak (nieuwe bron, bijna-match, naamverschil, onafhankelijke botteling zonder kandidaat, alleen als sample, overig) en schrijft een rapport buiten de repo.

```bash
python scripts/enrich_cascade.py --fetch-extra     # pagina's van de extra bronnen ophalen (cache) en de cascade draaien
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
python scripts/check_tags.py
python scripts/analyse_gaten.py --out /tmp/analyse.json
```

## Tags

Alle tags (regio, vat, stijl, kenmerken en smaak) worden in `export_site.py` berekend en staan als `tg` in de index. Lijst, kaarten, filters en detailpagina gebruiken exact die lijst; de lijst kan inkorten met "+N". `scripts/check_tags.py` faalt als de index afwijkt van wat uit de detailgegevens volgt.

Als een korte Whiskysite-stub (alleen maat/ABV of een kenmerkregel) wordt vervangen door een langere omschrijving van een lagere bron, blijven de kenmerkregels uit die stub bewaard (zonder maat/ABV-regel en zonder winkelmeldingen) als feit met bron Whiskysite. Bij een tegenstrijdig vat wint dat Whiskysite-feit; vattags komen dan niet uit de vervangende tekst.

Items die geen whisky zijn (rum, tequila, cognac of gin als drank, lege sampleflessen) worden bij het bouwen niet geëxporteerd, op basis van de markering *niet van toepassing* en een naamcheck. Een volle whiskyfles die per ongeluk in de samplecategorie stond, wordt als fles opgenomen (niet als sample).
