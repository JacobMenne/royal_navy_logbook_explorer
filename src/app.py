import csv
import json
import os
import threading
import webbrowser
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from db import get_connection, init_db
from ingest_cliwoc import (
    YEAR_MAX, YEAR_MIN, clean_val, detect_delimiter,
    extract_narrative, parse_year, scan_cliwoc_ship,
)
from ingest_corral import download_corral_logbook
from triage import extract_route_waypoints, generate_deep_dive, triage_ship_folder

BASE_DIR = Path(__file__).resolve().parent.parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ships", methods=["GET"])
def list_ships():
    source_type = request.args.get("source_type")
    query = """
        SELECT s.*, 
               CASE WHEN sv.ship_id IS NOT NULL THEN 1 ELSE 0 END AS is_saved
        FROM ships s
        LEFT JOIN saved_voyages sv ON s.id = sv.ship_id
    """
    params = []
    if source_type in ("structured", "raw_scan"):
        query += " WHERE s.source_type = ?"
        params.append(source_type)

    query += " ORDER BY (s.interest_score IS NULL) ASC, s.interest_score DESC, s.ship_name ASC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/library", methods=["GET"])
def get_library():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT s.*,
                   CASE WHEN sv.ship_id IS NOT NULL THEN 1 ELSE 0 END AS is_saved,
                   sv.saved_at
            FROM ships s
            LEFT JOIN saved_voyages sv ON s.id = sv.ship_id
            WHERE s.source_type = 'raw_scan'
              AND (sv.ship_id IS NOT NULL OR s.interest_score IS NOT NULL)
            ORDER BY is_saved DESC, s.interest_score DESC, s.ship_name ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

def build_ship_detail_payload(ship_id: int, scan_progress: dict | None = None):
    with get_connection() as conn:
        ship = conn.execute("""
            SELECT s.*, 
                   CASE WHEN sv.ship_id IS NOT NULL THEN 1 ELSE 0 END AS is_saved
            FROM ships s
            LEFT JOIN saved_voyages sv ON s.id = sv.ship_id
            WHERE s.id = ?
        """, (ship_id,)).fetchone()
        if not ship:
            return None

        pages = conn.execute("""
            SELECT page_num, date_range, lat_lon, remarks_summary, full_transcription, flagged_keywords, page_score
            FROM triage_pages
            WHERE ship_id = ?
            ORDER BY page_num ASC
        """, (ship_id,)).fetchall()

    flagged = []
    for p in pages:
        item = dict(p)
        item["flagged_keywords"] = json.loads(item["flagged_keywords"] or "[]")
        flagged.append(item)

    payload = {"ship": dict(ship), "flagged_pages": flagged}
    if scan_progress is not None:
        payload["scan_progress"] = scan_progress
    return payload

@app.route("/api/ships/<int:ship_id>", methods=["GET"])
def get_ship_detail(ship_id):
    payload = build_ship_detail_payload(ship_id)
    if not payload:
        return jsonify({"error": "Ship not found"}), 404
    return jsonify(payload)

@app.route("/api/ships/<int:ship_id>/triage", methods=["POST"])
def run_triage(ship_id):
    with get_connection() as conn:
        ship = conn.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if not ship:
            return jsonify({"error": "Ship not found"}), 404
        source_path = ship["source_path"]

    scan_progress = None
    try:
        if ship["source_type"] == "raw_scan":
            if source_path.strip().startswith(("[", "http://", "https://")):
                local_folder = download_corral_logbook(source_path, ship["ship_name"])
                scan_progress = triage_ship_folder(ship_id, str(local_folder), batch_size=25)
            else:
                source = Path(source_path)
                if not source.exists():
                    return jsonify({"error": f"Folder not found: {source}."}), 400
                scan_progress = triage_ship_folder(ship_id, str(source), batch_size=25)
        else:
            source = Path(source_path)
            if not source.exists():
                return jsonify({"error": f"Source not found: {source}."}), 400
            scan_cliwoc_ship(ship_id, ship["ship_name"], str(source))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(build_ship_detail_payload(ship_id, scan_progress=scan_progress))

@app.route("/api/ships/<int:ship_id>/save", methods=["POST"])
def save_voyage(ship_id):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_voyages (ship_id, notes) VALUES (?, ?)",
            (ship_id, "Bookmarked from Explorer UI")
        )
    return jsonify({"status": "saved", "ship_id": ship_id})

@app.route("/api/ships/<int:ship_id>/deep-dive", methods=["POST"])
def deep_dive(ship_id):
    try:
        generate_deep_dive(ship_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return get_ship_detail(ship_id)

@app.route("/api/ships/<int:ship_id>/route", methods=["GET"])
def get_ship_route(ship_id):
    with get_connection() as conn:
        ship = conn.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if not ship:
            return jsonify({"error": "Ship not found"}), 404

    try:
        if ship["source_type"] == "structured":
            path = Path(ship["source_path"])
            if not path.exists():
                return jsonify({"error": f"Source file not found: {path}"}), 400

            delimiter = detect_delimiter(path)
            waypoints = []
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                for row in reader:
                    if (row.get("ShipName") or "").strip() != ship["ship_name"]:
                        continue
                    year = parse_year(row)
                    if not year or not (YEAR_MIN <= year <= YEAR_MAX):
                        continue
                    try:
                        lat = float(clean_val(row.get("Lat3") or row.get("Lat")))
                        lon = float(clean_val(row.get("Lon3") or row.get("Lon")))
                    except ValueError:
                        continue

                    date_str = f"{row.get('Year', '?')}-{row.get('Month', '?')}-{row.get('Day', '?')}"
                    narrative = extract_narrative(row)
                    v_from = clean_val(row.get("VoyageFrom"))
                    v_to = clean_val(row.get("VoyageTo"))
                    place = f"{v_from} → {v_to}" if (v_from or v_to) else "At Sea"
                    waypoints.append({
                        "date": date_str,
                        "place_name": place,
                        "lat": lat,
                        "lon": lon,
                        "summary": narrative or "Routine daily position.",
                        "is_event": bool(narrative),
                    })
            return jsonify({"waypoints": waypoints})
        else:
            waypoints = extract_route_waypoints(ship_id)
            return jsonify({"waypoints": waypoints})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    threading.Timer(0.2, os._exit, args=[0]).start()
    return jsonify({"status": "shutting down"})

if __name__ == "__main__":
    init_db()
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=True, use_reloader=False, port=5000)