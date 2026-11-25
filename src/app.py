import json
import os
import threading
import webbrowser
from pathlib import Path
from flask import Flask, jsonify, render_template
from db import get_connection, init_db
from triage import triage_ship_folder

BASE_DIR = Path(__file__).resolve().parent.parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ships", methods=["GET"])
def list_ships():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT s.*, 
                   CASE WHEN sv.ship_id IS NOT NULL THEN 1 ELSE 0 END AS is_saved
            FROM ships s
            LEFT JOIN saved_voyages sv ON s.id = sv.ship_id
            ORDER BY s.ship_name ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/ships/<int:ship_id>", methods=["GET"])
def get_ship_detail(ship_id):
    with get_connection() as conn:
        ship = conn.execute("""
            SELECT s.*, 
                   CASE WHEN sv.ship_id IS NOT NULL THEN 1 ELSE 0 END AS is_saved
            FROM ships s
            LEFT JOIN saved_voyages sv ON s.id = sv.ship_id
            WHERE s.id = ?
        """, (ship_id,)).fetchone()
        if not ship:
            return jsonify({"error": "Ship not found"}), 404

        pages = conn.execute("""
            SELECT page_num, date_range, lat_lon, remarks_summary, flagged_keywords, page_score
            FROM triage_pages
            WHERE ship_id = ? AND page_score > 0
            ORDER BY page_score DESC, page_num ASC
        """, (ship_id,)).fetchall()

    flagged = []
    for p in pages:
        item = dict(p)
        item["flagged_keywords"] = json.loads(item["flagged_keywords"] or "[]")
        flagged.append(item)

    return jsonify({"ship": dict(ship), "flagged_pages": flagged})

@app.route("/api/ships/<int:ship_id>/triage", methods=["POST"])
def run_triage(ship_id):
    with get_connection() as conn:
        ship = conn.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if not ship:
            return jsonify({"error": "Ship not found"}), 404

        if ship["source_type"] != "raw_scan":
            return jsonify({"error": "Structured CLIWOC scanning not wired up yet."}), 400

        folder = Path(ship["source_path"])
        if not folder.exists():
            return jsonify({"error": f"Folder not found: {folder}. Add scan images there first."}), 400

        conn.execute("DELETE FROM triage_pages WHERE ship_id = ?", (ship_id,))

    triage_ship_folder(ship_id, str(folder))
    return get_ship_detail(ship_id)

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
    return jsonify({
        "status": "queued",
        "message": f"Deep Dive full transcription queued for ship ID {ship_id} (Phase 4)."
    })

@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    threading.Timer(0.2, os._exit, args=[0]).start()
    return jsonify({"status": "shutting down"})

if __name__ == "__main__":
    init_db()
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=True, use_reloader=False, port=5000)