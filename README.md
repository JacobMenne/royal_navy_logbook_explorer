# Royal Navy Logbook Explorer

A lightweight Python and Vision-LLM pipeline for scanning, scoring, and reconstructing historical narratives from 18th- and 19th-century Royal Navy ship logbooks (1756–1815).

## Features

- **Unified Ship Registry:** Indexes both raw handwritten logbook scans (JPEG/PNG) and structured historical datasets (CLIWOC 2.1) into a local SQLite database.
- **Vision-LLM Transcription & Scanning:** Uses Gemini Flash-Lite to read 18th/19th-century cursive, extract dates and coordinates, produce full verbatim transcriptions, and flag high-drama events (mutinies, engagements, floggings, desertions, prizes, storms).
- **0–10 Interest Scoring:** Ranks individual pages and entire voyages using a weighted keyword tier system so routine patrols are separated from notable journeys.
- **Parchment Web UI:** Minimalist Flask interface that auto-launches in your browser and shuts down cleanly when the tab is closed.

## Project Structure

```text
royal_navy_logbook_explorer/
├── data/
│   ├── raw_logs/          # Local folders of scanned logbook JPEGs/PNGs per ship
│   ├── structured/        # Structured datasets (e.g., CLIWOC CSV/TXT)
│   └── navy_logs.db       # Local SQLite database (auto-generated)
├── src/
│   ├── app.py             # Flask server and API endpoints
│   ├── db.py              # SQLite schema and connection helper
│   ├── triage.py          # Vision-LLM scanning and 0-10 scoring engine
│   ├── ingest_cliwoc.py   # CLIWOC 2.1 structured dataset ingester & scanner
│   └── seed.py            # Initial sample ship seeder
├── templates/
│   └── index.html         # Parchment-themed single-page web UI
├── requirements.txt
└── .env                   # Local Gemini API key (git-ignored)
```
