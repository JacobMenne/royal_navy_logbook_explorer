import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel
from db import get_connection

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

class PageTriage(BaseModel):
    detected_ship_name: str
    date_range: str
    lat_lon: str
    remarks_summary: str
    full_transcription: str
    flagged_keywords: list[str]

class Waypoint(BaseModel):
    date: str
    place_name: str
    lat: float
    lon: float
    summary: str
    is_event: bool

class RouteExtraction(BaseModel):
    waypoints: list[Waypoint]

KEYWORD_WEIGHTS = {
    # Tier 1: Critical Drama
    "mutiny": 5.0, "mutinous": 5.0, "sunk": 5.0, "wrecked": 5.0,
    "captured": 5.0, "struck colours": 5.0, "engagement": 5.0,
    "broadside": 5.0, "prize": 5.0,
    # Tier 2: High Drama / Discipline
    "flogged": 2.5, "punished": 2.5, "lashes": 2.5, "deserted": 2.5,
    "court martial": 2.5, "hanged": 2.5, "buried": 2.5, "killed": 2.5,
    "wounded": 2.5, "chased": 2.5,
    # Tier 3: Environmental / Notable
    "gale": 1.0, "hurricane": 1.0, "sighted": 1.0, "strange sail": 1.0,
    "leaking": 1.0, "short of water": 1.0, "sick": 1.0
}

PROMPT = """
You are a naval historian transcribing an 18th/19th-century Royal Navy ship logbook page.
Read the header, title page (if present), handwritten 'Remarks', and navigation columns carefully.
Extract:
1. detected_ship_name: The name of the Royal Navy ship if written on the cover, header, or page (e.g., "HMS Victory" or "HMS Indefatigable"), or "Unknown" if not visible.
2. date_range: The dates covered on this page (e.g., "1798-04-12 to 1798-04-15" or "Unknown").
3. lat_lon: Any latitude/longitude recorded, or "None".
4. remarks_summary: A 1-2 sentence summary of notable events (discipline, weather, sightings, combat).
5. full_transcription: A complete, faithful transcription of all readable handwritten text on the page, preserving line breaks and dates where possible. Use [illegible] for words that cannot be read.
6. flagged_keywords: Any exact or closely matching terms from this list that appear on the page:
   mutiny, mutinous, sunk, wrecked, captured, struck colours, engagement, broadside, prize,
   flogged, punished, lashes, deserted, court martial, hanged, buried, killed, wounded, chased,
   gale, hurricane, sighted, strange sail, leaking, short of water, sick.
"""

def score_page(keywords: list[str]) -> float:
    raw_score = sum(KEYWORD_WEIGHTS.get(k.lower(), 0.0) for k in keywords)
    return round(min(10.0, raw_score), 1)

def calculate_voyage_score(page_scores: list[float], all_keywords: set[str]) -> float:
    if not page_scores:
        return 1.0
    total_points = sum(page_scores)
    density_score = min(10.0, 1.0 + (total_points / max(1, len(page_scores))) * 3.0)

    if "mutiny" in all_keywords or "mutinous" in all_keywords:
        density_score = max(density_score, 8.5)
    elif any(k in all_keywords for k in ("engagement", "wrecked", "captured", "broadside")):
        density_score = max(density_score, 7.0)

    return round(density_score, 1)

def scan_page_with_retry(img: Image.Image, max_attempts: int = 3) -> PageTriage:
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[PROMPT, img],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PageTriage,
                    temperature=0.1,
                ),
            )
            return PageTriage.model_validate_json(response.text)
        except Exception as e:
            if attempt == max_attempts:
                raise
            wait = 3 * (2 ** (attempt - 1))  # 3s, 6s
            print(f"  Temporary API error ({e}). Retrying in {wait}s...", flush=True)
            time.sleep(wait)

def _scan_worker(idx: int, total_images: int, img_path: Path) -> tuple[int, PageTriage]:
    print(f"Scanning page {idx}/{total_images}: {img_path.name}...", flush=True)
    img = Image.open(img_path)
    img.thumbnail((1600, 1600))
    data = scan_page_with_retry(img)
    return idx, data

def triage_ship_folder(ship_id: int, folder_path: str, batch_size: int = 25, max_workers: int = 4) -> dict:
    """
    Parallel resumable scanner: skips already-scanned page_nums, scans up to
    `batch_size` new pages concurrently, and commits each page immediately.
    """
    image_files = sorted(
        p for p in Path(folder_path).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    total_images = len(image_files)
    if not image_files:
        print(f"No images found in {folder_path}", flush=True)
        return {"scanned_now": 0, "total_scanned": 0, "total_images": 0}

    with get_connection() as conn:
        existing_nums = {
            r["page_num"]
            for r in conn.execute("SELECT page_num FROM triage_pages WHERE ship_id = ?", (ship_id,)).fetchall()
        }

    to_scan = [
        (idx, img_path)
        for idx, img_path in enumerate(image_files, start=1)
        if idx not in existing_nums
    ]
    if batch_size:
        to_scan = to_scan[:batch_size]

    scanned_now = 0
    detected_names = []

    if to_scan:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_scan_worker, idx, total_images, img_path): (idx, img_path)
                for idx, img_path in to_scan
            }
            for future in as_completed(futures):
                idx, img_path = futures[future]
                try:
                    page_idx, data = future.result()
                except Exception as e:
                    print(f"  Skipping page {idx} ({img_path.name}) after retries: {e}", flush=True)
                    continue

                if data.detected_ship_name and data.detected_ship_name.strip().lower() not in {"unknown", "none", "n/a"}:
                    detected_names.append(data.detected_ship_name.strip())

                p_score = score_page(data.flagged_keywords)

                with get_connection() as conn:
                    conn.execute("""
                        INSERT INTO triage_pages 
                        (ship_id, page_num, date_range, lat_lon, remarks_summary, full_transcription, flagged_keywords, page_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        ship_id, page_idx, data.date_range, data.lat_lon,
                        data.remarks_summary, data.full_transcription,
                        json.dumps(data.flagged_keywords), p_score
                    ))

                scanned_now += 1

    # Recompute overall voyage summary, score, and metadata across ALL scanned pages
    with get_connection() as conn:
        all_pages = conn.execute("""
            SELECT page_num, date_range, remarks_summary, flagged_keywords, page_score
            FROM triage_pages
            WHERE ship_id = ?
            ORDER BY page_num ASC
        """, (ship_id,)).fetchall()

        page_scores = [p["page_score"] or 0.0 for p in all_pages]
        all_keywords = set()
        flagged_summaries = []
        detected_years = []

        for p in all_pages:
            kws = json.loads(p["flagged_keywords"] or "[]")
            all_keywords.update(k.lower() for k in kws)
            years = [int(y) for y in re.findall(r"\b(17\d{2}|18\d{2})\b", p["date_range"] or "")]
            detected_years.extend(years)
            if (p["page_score"] or 0) > 0:
                flagged_summaries.append(f"Page {p['page_num']} ({p['date_range']}): {p['remarks_summary']}")

        final_score = calculate_voyage_score(page_scores, all_keywords)
        header_line = f"Scanned {len(all_pages)} of {total_images} pages."
        if flagged_summaries:
            summary_text = header_line + "\n\n" + "\n".join(flagged_summaries[:12])
        else:
            summary_text = header_line + "\n\nRoutine voyage so far; no major incidents flagged."

        ship_row = conn.execute("SELECT ship_name, year_start, year_end FROM ships WHERE id = ?", (ship_id,)).fetchone()
        new_name = ship_row["ship_name"]
        new_start = ship_row["year_start"]
        new_end = ship_row["year_end"]

        if detected_names and ship_row["ship_name"].lower().startswith(("adm", "p")):
            best_name = max(set(detected_names), key=detected_names.count)
            new_name = f"{best_name} ({ship_row['ship_name']})"
            print(f"Auto-identified vessel: {new_name}", flush=True)

        if detected_years:
            new_start = min(detected_years)
            new_end = max(detected_years)

        conn.execute("""
            UPDATE ships
            SET ship_name = ?, year_start = ?, year_end = ?,
                interest_score = ?, triage_summary = ?, route_json = NULL,
                triaged_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_name, new_start, new_end, final_score, summary_text, ship_id))

    print(f"\nBatch complete: +{scanned_now} pages ({len(all_pages)}/{total_images} total). Score: {final_score}/10", flush=True)
    return {"scanned_now": scanned_now, "total_scanned": len(all_pages), "total_images": total_images}

def generate_deep_dive(ship_id: int) -> str:
    with get_connection() as conn:
        ship = conn.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if not ship:
            raise ValueError("Ship not found.")

        pages = conn.execute("""
            SELECT page_num, date_range, lat_lon, remarks_summary, full_transcription
            FROM triage_pages
            WHERE ship_id = ?
            ORDER BY page_num ASC
        """, (ship_id,)).fetchall()

    if not pages:
        raise ValueError("No scanned pages found. Run 'Scan Logbooks' first.")

    compiled_entries = []
    for p in pages:
        text = p["full_transcription"] or p["remarks_summary"] or ""
        compiled_entries.append(
            f"--- Page {p['page_num']} ({p['date_range']} | Pos: {p['lat_lon']}) ---\n{text}"
        )

    log_corpus = "\n\n".join(compiled_entries)

    deep_dive_prompt = f"""
You are a naval historian writing a compelling, accurate historical dossier based strictly on primary-source ship logbook entries.

Vessel: {ship['ship_name']}
Class/Info: {ship['ship_class']}
Years: {ship['year_start']}–{ship['year_end']}

Below are the sequential logbook entries:
{log_corpus}

Write a clear, engaging Deep Dive report with these sections:
### Voyage Overview
(2-3 sentences summarizing the ship's mission, timeframe, and general waters)

### Chronological Narrative
(a flowing, readable account of the voyage in order, weaving together navigation, weather, sightings, encounters, and daily shipboard life)

### Key Incidents & Highlights
(bullet points of the most notable events, hazards, or encounters with dates)

### Route & Places Mentioned
(bullet points of ports, islands, capes, or coordinates referenced in the logs)

Stay faithful to the logbook text—do not invent events not supported by the entries.
"""

    print(f"Generating Deep Dive narrative for {ship['ship_name']}...", flush=True)
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=deep_dive_prompt,
        config=types.GenerateContentConfig(temperature=0.3),
    )
    narrative = response.text.strip()

    with get_connection() as conn:
        conn.execute(
            "UPDATE ships SET deep_dive_narrative = ? WHERE id = ?",
            (narrative, ship_id),
        )

    print("Deep Dive narrative saved.", flush=True)
    return narrative

def extract_route_waypoints(ship_id: int) -> list[dict]:
    with get_connection() as conn:
        ship = conn.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if not ship:
            raise ValueError("Ship not found.")

        if ship["route_json"]:
            return json.loads(ship["route_json"])

        pages = conn.execute("""
            SELECT page_num, date_range, lat_lon, remarks_summary, page_score
            FROM triage_pages
            WHERE ship_id = ?
            ORDER BY page_num ASC
        """, (ship_id,)).fetchall()

    if not pages:
        raise ValueError("No scanned pages found. Run 'Scan Logbooks' first.")

    entries_text = "\n".join(
        f"Page {p['page_num']} ({p['date_range']} | Coords: {p['lat_lon']} | Score: {p['page_score']}): {p['remarks_summary']}"
        for p in pages
    )

    prompt = f"""
You are a historical maritime geographer. Based on the sequential logbook entries below for {ship['ship_name']} ({ship['year_start']}–{ship['year_end']}), extract an ordered list of geographical waypoints representing the ship's voyage.

STRICT GEOGRAPHICAL RULES:
1. This is an oceangoing sailing vessel—EVERY waypoint MUST be in open water, a coastal anchorage, or a seaport. NEVER place a waypoint inland (e.g., inside Paraguay, Argentina, etc.).
2. Use exact decimal latitude/longitude if recorded in the log (remember South latitudes and West longitudes are negative).
3. For open-sea entries without explicit coordinates (such as searching for shoals or sailing between ports), interpolate a realistic offshore maritime position along the sea track between the previous and next known coastal landmarks.
4. Omit cover pages, title pages, and blank pages that contain no location or movement.
5. Set is_event=true for notable incidents, sightings, hazards, or port arrivals.

Logbook entries:
{entries_text}
"""

    print(f"Extracting route waypoints for {ship['ship_name']}...", flush=True)
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteExtraction,
            temperature=0.1,
        ),
    )
    extracted = RouteExtraction.model_validate_json(response.text)
    waypoints = [w.model_dump() for w in extracted.waypoints]

    with get_connection() as conn:
        conn.execute(
            "UPDATE ships SET route_json = ? WHERE id = ?",
            (json.dumps(waypoints), ship_id),
        )

    return waypoints

if __name__ == "__main__":
    print("Ready. Import triage_ship_folder() or run via src/app.py.")