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

NARRATIVE_LABELS = {
    "WarsAndFightsMemo": "Wars & Fights",
    "LifeOnBoardMemo": "Life on Board",
    "CargoMemo": "Cargo Remarks",
    "EncRem": "Encounter Remarks",
    "OthRem": "Other Remarks",
}

FIELD_LABELS = {
    "ID": "Record ID",
    "RecID": "Entry ID",
    "InstName": "Archive Institution",
    "InstPlace": "Archive City",
    "InstLand": "Archive Country",
    "LogbookIdent": "Logbook Archive Reference",
    "LogbookLanguage": "Logbook Language",
    "Calendar": "Calendar System",
    "UTC": "UTC Time",
    "Year": "Year",
    "Month": "Month",
    "Day": "Day",
    "ShipName": "Ship Name",
    "ShipType": "Ship Class / Type",
    "Company": "Company / Service",
    "OtherShipInformation": "Additional Ship Information",
    "Nationality": "Nationality",
    "Nation": "Nationality",
    "Name1": "Captain / Officer 1",
    "Rank1": "Rank 1",
    "Name2": "Captain / Officer 2",
    "Rank2": "Rank 2",
    "VoyageFrom": "Port of Departure",
    "VoyageTo": "Port of Destination",
    "Lat": "Latitude (Raw)",
    "Lon": "Longitude (Raw)",
    "Lat3": "Latitude (Decimal Degrees)",
    "Lon3": "Longitude (Decimal Degrees)",
    "LatInd": "Latitude Indicator",
    "LonInd": "Longitude Indicator",
    "drLat": "Dead-Reckoning Latitude",
    "drLon": "Dead-Reckoning Longitude",
    "ZeroMeridian": "Prime Meridian Reference",
    "Landmark": "Landmark Sighted",
    "DistToLandmark": "Distance to Landmark",
    "DistUnits": "Distance Units",
    "Bearing": "Compass Bearing",
    "Course": "Ship Course",
    "Distance": "Distance Sailed",
    "WindDirection": "Wind Direction",
    "WindDirectionScale": "Wind Direction Scale",
    "AllWindDirections": "All Wind Directions",
    "WindForce": "Wind Force",
    "WindForceScale": "Wind Force Scale",
    "AllWindForces": "All Wind Forces",
    "Weather": "Weather",
    "Clouds": "Cloud Cover",
    "SeaState": "Sea State",
    "Gusts": "Gusts",
    "Rain": "Rain",
    "Fog": "Fog",
    "Snow": "Snow",
    "Thunder": "Thunder",
    "Hail": "Hail",
    "SeaIce": "Sea Ice",
    "BaroReading": "Barometer Reading",
    "BaroUnits": "Barometer Units",
    "AirTempReading": "Air Temperature",
    "AirTempUnits": "Air Temperature Units",
    "WaterTempReading": "Sea Water Temperature",
    "WaterTempUnits": "Sea Water Temperature Units",
    "Illustr": "Contains Logbook Illustration",
}

SKIP_EXTRA_COLS = {
    "Year", "Month", "Day", "ShipName", "ShipType", "Nationality", "Nation",
    "VoyageFrom", "VoyageTo", "Lat", "Lon", "Lat3", "Lon3",
    *NARRATIVE_LABELS.keys(),
}

def humanize_column(col: str) -> str:
    if col in FIELD_LABELS:
        return FIELD_LABELS[col]
    # Split CamelCase fallback (e.g., "ProbT1" -> "Prob T1")
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", col)
    return spaced.replace("_", " ").strip()

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

def clean_val(val: str | None) -> str:
    v = (val or "").strip()
    return "" if v.lower() in {"", "na", "nan", "null", "0", "none"} else v

def extract_narrative(row: dict) -> str:
    parts = []
    for col, label in NARRATIVE_LABELS.items():
        val = clean_val(row.get(col))
        if val:
            parts.append(f"{label}: {val}")
    return " | ".join(parts)

def format_full_record(row: dict, date_str: str, lat: str, lon: str) -> str:
    lines = [
        f"Date: {date_str}",
        f"Latitude: {lat or 'Unknown'}",
        f"Longitude: {lon or 'Unknown'}",
        f"Port of Departure: {clean_val(row.get('VoyageFrom')) or 'Unknown'}",
        f"Port of Destination: {clean_val(row.get('VoyageTo')) or 'Unknown'}",
    ]

    for col, label in NARRATIVE_LABELS.items():
        val = clean_val(row.get(col))
        if val:
            lines.append(f"{label}: {val}")

    extras = []
    for col, raw_val in row.items():
        if col in SKIP_EXTRA_COLS:
            continue
        val = clean_val(raw_val)
        if val:
            extras.append(f"{humanize_column(col)}: {val}")

    if extras:
        lines.append("\n--- Additional Recorded Fields ---")
        lines.extend(extras)

    return "\n".join(lines)

def find_keywords(text: str) -> list[str]:
    lower = text.lower()
    hits = []
    for kw in KEYWORD_WEIGHTS:
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            hits.append(kw)
    return hits

def build_voyage_summary(matching_rows: list[dict], unique_events: list[dict]) -> str:
    routes = []
    seen_routes = set()
    lats, lons = [], []

    for r in matching_rows:
        v_from = clean_val(r.get("VoyageFrom"))
        v_to = clean_val(r.get("VoyageTo"))
        if v_from or v_to:
            route = f"{v_from or '?'} → {v_to or '?'}"
            if route not in seen_routes:
                seen_routes.add(route)
                routes.append(route)
        try:
            lat = float(clean_val(r.get("Lat3") or r.get("Lat")))
            lon = float(clean_val(r.get("Lon3") or r.get("Lon")))
            lats.append(lat)
            lons.append(lon)
        except ValueError:
            pass

    first_date = f"{matching_rows[0].get('Year', '?')}-{matching_rows[0].get('Month', '?')}-{matching_rows[0].get('Day', '?')}"
    last_date = f"{matching_rows[-1].get('Year', '?')}-{matching_rows[-1].get('Month', '?')}-{matching_rows[-1].get('Day', '?')}"

    lines = [f"Logged {len(matching_rows)} daily entries ({first_date} to {last_date})."]
    if routes:
        lines.append("Routes: " + "; ".join(routes[:5]))
    if lats and lons:
        lines.append(f"Operating Area: Latitude {min(lats):.1f}° to {max(lats):.1f}°, Longitude {min(lons):.1f}° to {max(lons):.1f}°.")

    flagged = [e for e in unique_events if e["score"] > 0]
    if flagged:
        lines.append("\nNotable Events:")
        for e in flagged[:8]:
            lines.append(f"• {e['date_range']}: {e['summary']}")
    else:
        lines.append("\nNo major combat, discipline, or crisis keywords flagged.")

    return "\n".join(lines)

def process_ship_rows(conn, ship_id: int, matching_rows: list[dict]) -> tuple[float, int]:
    conn.execute("DELETE FROM triage_pages WHERE ship_id = ?", (ship_id,))

    unique_events = []
    for idx, row in enumerate(matching_rows, start=1):
        date_str = f"{row.get('Year', '?')}-{row.get('Month', '?')}-{row.get('Day', '?')}"
        lat = clean_val(row.get("Lat3") or row.get("Lat"))
        lon = clean_val(row.get("Lon3") or row.get("Lon"))
        lat_lon = f"{lat}, {lon}" if lat and lon else "None"

        narrative = extract_narrative(row)
        if not narrative:
            continue

        if unique_events and unique_events[-1]["narrative"] == narrative:
            unique_events[-1]["end_date"] = date_str
            unique_events[-1]["days"] += 1
            continue

        keywords = find_keywords(narrative)
        p_score = score_page(keywords)
        unique_events.append({
            "page_num": idx,
            "start_date": date_str,
            "end_date": date_str,
            "days": 1,
            "lat_lon": lat_lon,
            "narrative": narrative,
            "full_record": format_full_record(row, date_str, lat, lon),
            "keywords": keywords,
            "score": p_score,
        })

    page_scores = []
    all_keywords = set()

    for ev in unique_events:
        if ev["days"] > 1:
            ev["date_range"] = f"{ev['start_date']} to {ev['end_date']} ({ev['days']} days)"
        else:
            ev["date_range"] = ev["start_date"]

        ev["summary"] = ev["narrative"][:240]
        page_scores.append(ev["score"])
        all_keywords.update(ev["keywords"])

        conn.execute(
            """
            INSERT INTO triage_pages
            (ship_id, page_num, date_range, lat_lon, remarks_summary, full_transcription, flagged_keywords, page_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ship_id, ev["page_num"], ev["date_range"], ev["lat_lon"],
                ev["summary"], ev["full_record"], json.dumps(ev["keywords"]), ev["score"],
            ),
        )

    total_slots = max(len(matching_rows) // 5, len(page_scores), 1)
    padded_scores = page_scores + [0.0] * max(0, total_slots - len(page_scores))
    final_score = calculate_voyage_score(padded_scores, all_keywords)
    summary_text = build_voyage_summary(matching_rows, unique_events)

    conn.execute(
        """
        UPDATE ships
        SET interest_score = ?, triage_summary = ?, triaged_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (final_score, summary_text, ship_id),
    )
    return final_score, len([e for e in unique_events if e["score"] > 0])

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

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        matching_rows = [
            r for r in reader
            if (r.get("ShipName") or "").strip() == ship_name
            and (y := parse_year(r)) is not None
            and YEAR_MIN <= y <= YEAR_MAX
        ]

    if not matching_rows:
        return

    with get_connection() as conn:
        final_score, hits = process_ship_rows(conn, ship_id, matching_rows)
    print(f"CLIWOC scan complete for {ship_name}: {len(matching_rows)} entries, {hits} unique flagged events, Score {final_score}/10")

def batch_scan_cliwoc(csv_path: str):
    path = Path(csv_path)
    if not path.exists():
        print(f"File not found: {path}")
        return

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ship_name FROM ships WHERE source_type = 'structured'"
        ).fetchall()
    ship_map = {r["ship_name"]: r["id"] for r in rows}

    if not ship_map:
        print("No structured ships found in DB. Run ingestion first.")
        return

    print(f"Grouping entries for {len(ship_map)} ships in a single pass...")
    delimiter = detect_delimiter(path)
    ship_rows = defaultdict(list)

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            name = (row.get("ShipName") or "").strip()
            if name in ship_map:
                year = parse_year(row)
                if year and YEAR_MIN <= year <= YEAR_MAX:
                    ship_rows[name].append(row)

    results = []
    with get_connection() as conn:
        for name, ship_id in ship_map.items():
            matching_rows = ship_rows.get(name, [])
            if not matching_rows:
                continue
            final_score, hits = process_ship_rows(conn, ship_id, matching_rows)
            results.append((final_score, name, hits))

    results.sort(key=lambda x: (-x[0], x[1]))
    print(f"\nBatch scan complete! Scored {len(results)} ships. Top 10:")
    for score, name, hits in results[:10]:
        print(f"  [{score}/10] {name} ({hits} unique flagged events)")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/ingest_cliwoc.py data/structured/CLIWOC21.tsv [--scan-all]")
    elif "--scan-all" in sys.argv:
        batch_scan_cliwoc(sys.argv[1])
    else:
        ingest_cliwoc(sys.argv[1])