# Whisky proefoverzicht

Statische pagina met samples van Whiskysite, scores van Whiskybase en flesprijzen in Nederland. Geschikt voor GitHub Pages: alleen `index.html` en `data.json`.

Elke botteling heeft een eigen detailpagina op `#/whisky/<id>`, met alle gegevens en pas daar de links naar externe sites. De naam in het overzicht opent die pagina.

## Data bijwerken

Het Excel-bestand blijft de bron. Persoonlijke kolommen (`Past bij smaak`, `Toelichting smaak`) worden niet geëxporteerd. Bij het genereren krijgt elke rij een stabiele `id` (URL-slug).

```bash
python scripts/build_data.py pad/naar/whisky_overzicht.xlsx
```

Dit schrijft `data.json` in de root van de repository. Ontbrekende velden blijven leeg of `onbekend`; er wordt niets verzonnen.
