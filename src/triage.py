import json
import os
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
Read the handwritten 'Remarks' and navigation columns carefully.
Extract:
1. date_range: The dates covered on this page (e.g., "1798-04-12 to 1798-04-15" or "Unknown").
2. lat_lon: Any latitude/longitude recorded, or "None".
3. remarks_summary: A 1-2 sentence summary of notable events (discipline, weather, sightings, combat).
4. full_transcription: A complete, faithful transcription of all readable handwritten text on the page, preserving line breaks and dates where possible. Use [illegible] for words that cannot be read.
5. flagged_keywords: Any exact or closely matching terms from this list that appear on the page:
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

def triage_ship_folder(ship_id: int, folder_path: str):
    image_files = sorted(
        p for p in Path(folder_path).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not image_files:
        print(f"No images found in {folder_path}")
        return

    page_scores = []
    all_keywords = set()
    flagged_summaries = []

    with get_connection() as conn:
        for idx, img_path in enumerate(image_files, start=1):
            print(f"Scanning page {idx}/{len(image_files)}: {img_path.name}...")
            img = Image.open(img_path)

            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[PROMPT, img],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PageTriage,
                    temperature=0.1,
                ),
            )

            data = PageTriage.model_validate_json(response.text)
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

        final_score = calculate_voyage_score(page_scores, all_keywords)
        summary_text = "\n".join(flagged_summaries[:10]) if flagged_summaries else "Routine voyage; no major incidents flagged."

        conn.execute("""
            UPDATE ships
            SET interest_score = ?, triage_summary = ?, triaged_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (final_score, summary_text, ship_id))

    print(f"\nScan complete! Final Interest Score: {final_score}/10")

if __name__ == "__main__":
    print("Ready. Import triage_ship_folder() or run via src/app.py.")