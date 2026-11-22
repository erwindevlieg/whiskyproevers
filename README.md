# Whisky proefoverzicht

Statische pagina met samples van Whiskysite, scores van Whiskybase en flesprijzen in Nederland. Geschikt voor GitHub Pages: `index.html`, `data.json`, `descriptions.json` en `assets/app.css`.

Elke botteling heeft een eigen detailpagina op `#/whisky/<id>`, met alle gegevens en pas daar de links naar externe sites. De naam in het overzicht opent die pagina. Via **Vergelijk** (twee of drie flessen, ook te delen als `#/vergelijk/<id>,<id>`) zet je ze naast elkaar. `#/inzicht` toont een spreidingsdiagram van score versus sampleprijs, alleen voor rijen waarvan beide bekend zijn.

Het uiterlijk volgt de tokens en componentstijl van [shadcn/ui](https://ui.shadcn.com/) (zinc, radius 0,5rem). Licht, donker of systeem kies je zelf; de standaard volgt de instelling van het apparaat. Het overzicht heeft een compacte lijst en een kaartweergave. De CSS wordt lokaal gebouwd en meegecommitteerd; er is geen runtime-CDN voor styles.

```bash
npx tailwindcss@3 -c styles/tailwind.config.js -i styles/input.css -o assets/app.css --minify
```

## Data bijwerken

Het Excel-bestand blijft de bron. Persoonlijke kolommen (`Past bij smaak`, `Toelichting smaak`) worden niet geëxporteerd. Bij het genereren krijgt elke rij een stabiele `id` (URL-slug).

Productomschrijvingen van Whiskysite.nl en productfoto-URL’s haal je apart op (ruwe HTML wordt gecached, opnieuw draaien haalt alleen ontbrekende pagina’s op). Foto’s worden alleen als URL opgeslagen, niet als bestand.

```bash
python scripts/fetch_descriptions.py
python scripts/fetch_images.py
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
```

`fetch_descriptions.py` schrijft `descriptions.json`. `fetch_images.py` vult daarin `image` (hotlink) met deze volgorde: flespagina (Whiskysite of andere winkel), daarna een zekere Whiskysite-zoekmatch op de fles, daarna Drankdozijn (alleen zekere naam-match; live-pagina’s van Drankdozijn zijn vaak Cloudflare-geblokkeerd voor kale HTTP, daarom worden gerenderde/gecachte pagina’s gebruikt), daarna Whiskybase (alleen bij WB-status exact of labelversie; bij een Cloudflare-blokkade wordt live overgeslagen), en pas daarna een non-generieke samplefoto. Generieke 3/6 cl-sampleplaatjes worden overgeslagen. `build_data.py` voegt `omschrijving`, `omschrijvingBron` en `afbeelding` toe wanneer die er zijn. Omschrijvingen worden letterlijk overgenomen, niet herschreven. De pagina laadt `descriptions.json` op de detailweergave als fallback voor de tekst.

Het blok Omschrijving op de detailpagina verschijnt alleen als er een shoptekst is; andere ontbrekende velden blijven leeg of `onbekend`. Er wordt niets verzonnen.
