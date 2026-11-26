import sqlite3
from pathlib import Path

DB_PATH = Path("data/navy_logs.db")

def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_name TEXT NOT NULL,
                ship_class TEXT,
                year_start INTEGER,
                year_end INTEGER,
                source_type TEXT NOT NULL CHECK(source_type IN ('raw_scan', 'structured')),
                source_path TEXT NOT NULL,
                interest_score REAL,
                triage_summary TEXT,
                deep_dive_narrative TEXT,
                route_json TEXT,
                triaged_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS triage_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL,
                page_num INTEGER NOT NULL,
                date_range TEXT,
                lat_lon TEXT,
                remarks_summary TEXT,
                full_transcription TEXT,
                flagged_keywords TEXT, -- JSON list
                page_score REAL,
                FOREIGN KEY (ship_id) REFERENCES ships(id)
            );

            CREATE TABLE IF NOT EXISTS saved_voyages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER UNIQUE NOT NULL,
                notes TEXT,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ship_id) REFERENCES ships(id)
            );
        """)
        # Migrate existing DB columns if missing
        page_cols = [r["name"] for r in conn.execute("PRAGMA table_info(triage_pages)").fetchall()]
        if "full_transcription" not in page_cols:
            conn.execute("ALTER TABLE triage_pages ADD COLUMN full_transcription TEXT")

        ship_cols = [r["name"] for r in conn.execute("PRAGMA table_info(ships)").fetchall()]
        if "deep_dive_narrative" not in ship_cols:
            conn.execute("ALTER TABLE ships ADD COLUMN deep_dive_narrative TEXT")
        if "route_json" not in ship_cols:
            conn.execute("ALTER TABLE ships ADD COLUMN route_json TEXT")

    print(f"Database initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()