from db import get_connection, init_db

SAMPLE_SHIPS = [
    ("HMS Victory", "1st Rate", 1803, 1805, "raw_scan", "data/raw_logs/hms_victory"),
    ("HMS Indefatigable", "Frigate", 1796, 1799, "raw_scan", "data/raw_logs/hms_indefatigable"),
    ("HMS Speedy", "Brig-sloop", 1800, 1801, "structured", "data/structured/cliwoc.csv"),
]

def seed():
    init_db()
    with get_connection() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
        if existing > 0:
            print("Database already has ships. Skipping seed.")
            return
        conn.executemany("""
            INSERT INTO ships (ship_name, ship_class, year_start, year_end, source_type, source_path)
            VALUES (?, ?, ?, ?, ?, ?)
        """, SAMPLE_SHIPS)
    print(f"Seeded {len(SAMPLE_SHIPS)} ships into data/navy_logs.db")

if __name__ == "__main__":
    seed()