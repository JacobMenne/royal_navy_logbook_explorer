import json
import os
import re
import time
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

def triage_ship_folder(ship_id: int, folder_path: str):
    image_files = sorted(
        p for p in Path(folder_path).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not image_files:
        print(f"No images found in {folder_path}", flush=True)
        return

    page_scores = []
    all_keywords = set()
    flagged_summaries = []
    detected_names = []
    detected_years = []

    with get_connection() as conn:
        for idx, img_path in enumerate(image_files, start=1):
            print(f"Scanning page {idx}/{len(image_files)}: {img_path.name}...", flush=True)
            try:
                img = Image.open(img_path)
                img.thumbnail((1600, 1600))
                data = scan_page_with_retry(img)
            except Exception as e:
                print(f"  Skipping page {idx} ({img_path.name}) after retries: {e}", flush=True)
                continue

            if data.detected_ship_name and data.detected_ship_name.strip().lower() not in {"unknown", "none", "n/a"}:
                detected_names.append(data.detected_ship_name.strip())

            years_in_page = [int(y) for y in re.findall(r"\b(17\d{2}|18\d{2})\b", data.date_range)]
            detected_years.extend(years_in_page)

            p_score = score_page(data.flagged_keywords)
            page_scores.append(p_score)
            all_keywords.update(k.lower() for k in data.flagged_keywords)

            if p_score > 0:
                flagged_summaries.append(f"Page {idx} ({data.date_range}): {data.remarks_summary}")

            conn.execute("""
                INSERT INTO triage_pages 
                (ship_id, page_num, date_range, lat_lon, remarks_summary, full_transcription, flagged_keywords, page_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ship_id, idx, data.date_range, data.lat_lon,
                data.remarks_summary, data.full_transcription,
                json.dumps(data.flagged_keywords), p_score
            ))

            time.sleep(1.0)

        final_score = calculate_voyage_score(page_scores, all_keywords)
        summary_text = "\n".join(flagged_summaries[:10]) if flagged_summaries else "Routine voyage; no major incidents flagged."

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
                interest_score = ?, triage_summary = ?, triaged_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_name, new_start, new_end, final_score, summary_text, ship_id))

    print(f"\nScan complete! Final Interest Score: {final_score}/10", flush=True)

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
1. Voyage Overview (2-3 sentences summarizing the ship's mission, timeframe, and general waters).
2. Chronological Narrative (a flowing, readable account of the voyage in order, weaving together navigation, weather, sightings, encounters, and daily shipboard life).
3. Key Incidents & Highlights (bullet points of the most notable events, hazards, or encounters with dates).
4. Route & Places Mentioned (ports, islands, capes, or coordinates referenced in the logs).

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

if __name__ == "__main__":
    print("Ready. Import triage_ship_folder() or run via src/app.py.")