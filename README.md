# Whisky proefoverzicht

Statische pagina met samples van Whiskysite, scores van Whiskybase en flesprijzen in Nederland. Geschikt voor GitHub Pages: `index.html`, `data.json`, `descriptions.json` en `assets/app.css`.

Elke botteling heeft een eigen detailpagina op `#/whisky/<id>`, met alle gegevens en pas daar de links naar externe sites. De naam in het overzicht opent die pagina.

Het uiterlijk volgt de tokens en componentstijl van [shadcn/ui](https://ui.shadcn.com/) (zinc, radius 0,5rem) en schakelt automatisch naar donkere modus via `prefers-color-scheme`. De CSS wordt lokaal gebouwd en meegecommitteerd; er is geen runtime-CDN voor styles.

```bash
npx tailwindcss@3 -c styles/tailwind.config.js -i styles/input.css -o assets/app.css --minify
```

## Data bijwerken

Het Excel-bestand blijft de bron. Persoonlijke kolommen (`Past bij smaak`, `Toelichting smaak`) worden niet geëxporteerd. Bij het genereren krijgt elke rij een stabiele `id` (URL-slug).

Productomschrijvingen van Whiskysite.nl haal je apart op (ruwe HTML wordt gecached, opnieuw draaien haalt alleen ontbrekende pagina’s op):

```bash
python scripts/fetch_descriptions.py
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
```

`fetch_descriptions.py` schrijft `descriptions.json`. `build_data.py` voegt de velden `omschrijving` en `omschrijvingBron` toe wanneer die er zijn. Omschrijvingen worden letterlijk overgenomen, niet herschreven. De pagina laadt `descriptions.json` op de detailweergave.

Het blok Omschrijving op de detailpagina verschijnt alleen als er een shoptekst is; andere ontbrekende velden blijven leeg of `onbekend`. Er wordt niets verzonnen.
