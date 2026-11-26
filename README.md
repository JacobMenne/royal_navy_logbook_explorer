# Royal Navy Logbook Explorer

A local web application for ingesting, transcribing, scoring, and mapping historical Royal Navy ship logbooks (1756–1830s). It pairs instant local analysis of structured maritime datasets (**CLIWOC 2.1**) with on-demand Vision-LLM transcription of raw handwritten Admiralty logbook scans (**CEDA CORRAL**).

## Features

- **Dual-Source Exploration:** Toggle between the structured **CLIWOC Archive** and **Handwritten Scans (Vision-LLM)** from the CEDA CORRAL (`ADM 51` / `ADM 53`) archives.
- **Volume Grouping & Resumable Scanning:** Automatically groups multi-part Admiralty folders (e.g., `p3054-1` through `p3054-9`) into a single unified voyage. Scans in resumable 25-page batches with per-page SQLite commits, parallel downloads, and Free Tier rate-limit pacing.
- **Automated Vessel Identification & Scoring:** Reads handwritten cover and title pages to auto-name vessels and date ranges, then scores pages (1–10) for notable events (mutinies, wrecks, engagements, discipline, gales, and sightings).
- **Compact Dossier UI:** Displays a scrollable Top-8 severity **Voyage Summary**, collapsible 25-page batches with full verbatim transcriptions, and a dedicated **Saved & Scanned** library view.
- **Deep Dive Narratives:** Synthesizes sequential logbook transcriptions into a formatted historical report (Voyage Overview, Chronological Narrative, Key Incidents, and Places Mentioned), cached in SQLite.
- **Interactive Route Maps (Leaflet.js):** Plots daily coordinates and offshore-constrained historical waypoints with custom **START** / **END** badges and clickable event pins.

## Tech Stack

- **Backend:** Python, Flask, SQLite (`data/navy_logs.db`)
- **Frontend:** Vanilla HTML/CSS/JS, Leaflet.js
- **AI / Vision-LLM:** Google Gemini API (`gemini-3.5-flash-lite`) via `google-genai` and Pydantic structured outputs
- **Data Sources:** CLIWOC 2.1 (`CLIWOC21.tsv`) & CEDA CORRAL Admiralty Logbook Scans

## Quick Start (Browse Pre-Scanned Voyages)

`data/navy_logs.db` is included in this repository, so you can explore already-scanned voyages, read full transcriptions, and view route maps immediately **without downloading raw scans or configuring an API key**.

```bash
git clone [https://github.com/](https://github.com/)<your-username>/royal_navy_logbook_explorer.git
cd royal_navy_logbook_explorer

python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python src/app.py
```
