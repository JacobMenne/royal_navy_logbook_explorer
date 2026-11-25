import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from db import get_connection, init_db
from triage import KEYWORD_WEIGHTS, calculate_voyage_score, score_page

YEAR_MIN = 1756
YEAR_MAX = 1815
MIN_ENTRIES = 5

# Remarks & event columns present in CLIWOC 2.1 exports
REMARK_COLUMNS = [
    "WarsAndFightsMemo",
    "LifeOnBoardMemo",
    "CargoMemo",
    "EncRem",
    "OthRem",
    "Weather",
    "Gusts",
    "Rain",
    "Fog",
    "Snow",
    "Thunder",
    "Hail",
    "SeaIce",
]

def detect_delimiter(file_path: Path) -> str:
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        header = f.readline()
    return "\t" if header.count("\t") > header.count(",") else ","

def is_british(row: dict) -> bool:
    nat = (row.get("Nationality") or row.get("Nation") or "").strip().lower()
    return any(tag in nat for tag in ("british", "uk", "united kingdom", "english", "gb"))

def parse_year(row: dict) -> int | None:
    raw = (row.get("Year") or "").strip()
    try:
        return int(float(raw))
    except ValueError:
        return None

def extract_remarks(row: dict) -> str:
    parts = []
    for col in REMARK_COLUMNS:
        val = (row.get(col) or "").strip()
        if val and val.lower() not in {"na", "nan", "null", "0", "none"}:
            parts.append(f"{col}: {val}")
    return " | ".join(parts)

def find_keywords(text: str) -> list[str]:
    lower = text.lower()
    hits = []
    for kw in KEYWORD_WEIGHTS:
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            hits.append(kw)
    return hits

def ingest_cliwoc(csv_path: str):
    path = Path(csv_path)
    if not path.exists():
        print(f"File not found: {path}")
        return

    init_db()
    delimiter = detect_delimiter(path)
    ships_meta = defaultdict(lambda: {"years": [], "ship_type": "", "count": 0})

    print(f"Reading {path.name}...")
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            if not is_british(row):
                continue
            year = parse_year(row)
            if not year or not (YEAR_MIN <= year <= YEAR_MAX):
                continue

            name = (row.get("ShipName") or "").strip()
            if not name or name.lower() in {"na", "unknown"}:
                continue

            entry = ships_meta[name]
            entry["years"].append(year)
            entry["count"] += 1
            if not entry["ship_type"]:
                entry["ship_type"] = (row.get("ShipType") or "").strip()

    inserted = 0
    with get_connection() as conn:
        for name, meta in sorted(ships_meta.items()):
            if meta["count"] < MIN_ENTRIES:
                continue
            exists = conn.execute(
                "SELECT id FROM ships WHERE ship_name = ? AND source_type = 'structured'",
                (name,),
            ).fetchone()
            if exists:
                continue

            conn.execute(
                """
                INSERT INTO ships (ship_name, ship_class, year_start, year_end, source_type, source_path)
                VALUES (?, ?, ?, ?, 'structured', ?)
                """,
                (
                    name,
                    meta["ship_type"] or "Unknown",
                    min(meta["years"]),
                    max(meta["years"]),
                    str(path),
                ),
            )
            inserted += 1

    print(f"Ingestion complete: added {inserted} British ships (1756–1815) to data/navy_logs.db.")

def scan_cliwoc_ship(ship_id: int, ship_name: str, csv_path: str):
    path = Path(csv_path)
    delimiter = detect_delimiter(path)

    page_scores = []
    all_keywords = set()
    flagged_summaries = []

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        matching_rows = [
            r for r in reader
            if (r.get("ShipName") or "").strip() == ship_name
            and (y := parse_year(r)) is not None
            and YEAR_MIN <= y <= YEAR_MAX
        ]

    with get_connection() as conn:
        for idx, row in enumerate(matching_rows, start=1):
            date_str = f"{row.get('Year', '?')}-{row.get('Month', '?')}-{row.get('Day', '?')}"
            lat = (row.get("Lat3") or row.get("Lat") or "").strip()
            lon = (row.get("Lon3") or row.get("Lon") or "").strip()
            lat_lon = f"{lat}, {lon}" if lat and lon else "None"

            remarks = extract_remarks(row)
            keywords = find_keywords(remarks)
            p_score = score_page(keywords)

            page_scores.append(p_score)
            all_keywords.update(keywords)

            # Store only notable entries (or first 50 if routine) to keep SQLite lean
            if p_score > 0:
                summary = remarks[:240]
                flagged_summaries.append(f"Entry {idx} ({date_str}): {summary}")
                conn.execute(
                    """
                    INSERT INTO triage_pages
                    (ship_id, page_num, date_range, lat_lon, remarks_summary, full_transcription, flagged_keywords, page_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ship_id, idx, date_str, lat_lon,
                        summary, remarks, json.dumps(keywords), p_score,
                    ),
                )

        final_score = calculate_voyage_score(page_scores, all_keywords)
        summary_text = (
            "\n".join(flagged_summaries[:10])
            if flagged_summaries
            else f"Scanned {len(matching_rows)} daily CLIWOC entries; routine voyage with no major incidents flagged."
        )

        conn.execute(
            """
            UPDATE ships
            SET interest_score = ?, triage_summary = ?, triaged_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (final_score, summary_text, ship_id),
        )

    print(f"CLIWOC scan complete for {ship_name}: {len(matching_rows)} entries, Score {final_score}/10")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/ingest_cliwoc.py data/structured/CLIWOC15.csv")
    else:
        ingest_cliwoc(sys.argv[1])