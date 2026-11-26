import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse
from db import get_connection, init_db

CORRAL_BASE_URL = "https://data.ceda.ac.uk/badc/corral/images/"
RAW_LOGS_DIR = Path("data/raw_logs")
USER_AGENT = "RoyalNavyLogbookExplorer/1.0"

SKIP_EXTENSIONS = (
    ".txt", ".html", ".htm", ".pdf", ".xml", ".json", ".csv", ".tsv",
    ".css", ".js", ".png", ".gif", ".ico", ".svg", ".zip", ".tar", ".gz",
)

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)

def fetch_links(url: str) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    parser = LinkParser()
    parser.feed(html)
    return parser.links

def extract_years(text: str) -> tuple[int | None, int | None]:
    years = [int(y) for y in re.findall(r"\b(17\d{2}|18\d{2})\b", text)]
    if not years:
        return None, None
    return min(years), max(years)

def normalize_dir_url(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def format_elapsed(start_time: float) -> str:
    secs = int(time.time() - start_time)
    return f"{secs // 60:02d}:{secs % 60:02d}"

def parse_volume_key(url: str) -> tuple[str, int]:
    """
    Groups multi-part folders like .../adm51/p3054-3/ into ('adm51 - p3054', 3).
    Single-part folders like .../adm53/p0078/ become ('adm53 - p0078', 1).
    """
    parts = [unquote(p) for p in urlparse(url).path.strip("/").split("/") if p]
    series = parts[-2] if len(parts) >= 2 else "corral"
    folder = parts[-1]

    match = re.match(r"^(.+?)-(\d+)$", folder)
    if match:
        base_code = match.group(1)
        part_num = int(match.group(2))
    else:
        base_code = folder
        part_num = 1

    volume_label = f"{series} - {base_code}".replace("_", " ").strip()
    return volume_label, part_num

def crawl_corral(base_url: str = CORRAL_BASE_URL, max_depth: int = 4, limit: int = 30):
    init_db()
    base_url = normalize_dir_url(base_url)
    grouped_volumes = defaultdict(list)
    stack = [(base_url, 0)]
    visited = set()
    folders_found = 0
    start_time = time.time()

    print(f"Indexing CEDA/CORRAL archive from {base_url} (folder limit={limit})...", flush=True)

    while stack and folders_found < limit:
        url, depth = stack.pop()
        if url in visited:
            continue
        visited.add(url)

        short_path = urlparse(url).path.replace("/badc/corral/images", "") or "/"
        print(f"[{format_elapsed(start_time)}] Checking (depth {depth}): {short_path}", flush=True)

        try:
            links = fetch_links(url)
        except Exception as e:
            print(f"[{format_elapsed(start_time)}]   Skipping {short_path}: {e}", flush=True)
            continue

        resolved = [
            urljoin(url, l).split("#")[0].split("?")[0]
            for l in links
            if l and not l.startswith(("#", "?", "mailto:", "javascript:"))
        ]
        jpg_links = sorted({u for u in resolved if u.lower().endswith((".jpg", ".jpeg"))})

        if jpg_links:
            vol_label, part_num = parse_volume_key(url)
            grouped_volumes[vol_label].append({
                "part_num": part_num,
                "url": url,
                "page_count": len(jpg_links),
            })
            folders_found += 1
            print(
                f"[{format_elapsed(start_time)}]   FOUND ({folders_found}/{limit}): "
                f"{vol_label} [Part {part_num}] ({len(jpg_links)} pages)",
                flush=True,
            )
            continue

        if depth < max_depth:
            children = []
            for full_url in resolved:
                if full_url.lower().endswith(SKIP_EXTENSIONS) or full_url.lower().endswith((".jpg", ".jpeg")):
                    continue
                if "_medium" in full_url:
                    continue
                child_url = normalize_dir_url(full_url)
                if child_url.startswith(url) and child_url != url and child_url not in visited:
                    children.append((child_url, depth + 1))
            stack.extend(reversed(children))

    inserted = 0
    updated = 0
    with get_connection() as conn:
        for vol_label, parts in sorted(grouped_volumes.items()):
            parts.sort(key=lambda x: x["part_num"])
            urls = [p["url"] for p in parts]
            total_pages = sum(p["page_count"] for p in parts)
            part_count = len(parts)
            source_json = json.dumps(urls)
            y_start, y_end = extract_years(vol_label)
            ship_class = f"CORRAL ({part_count} {'part' if part_count == 1 else 'parts'}, {total_pages} pages)"

            existing = conn.execute(
                "SELECT id FROM ships WHERE ship_name LIKE ? AND source_type = 'raw_scan'",
                (f"%{vol_label}%",),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE ships SET ship_class = ?, source_path = ? WHERE id = ?",
                    (ship_class, source_json, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO ships (ship_name, ship_class, year_start, year_end, source_type, source_path)
                    VALUES (?, ?, ?, ?, 'raw_scan', ?)
                    """,
                    (vol_label, ship_class, y_start or 1756, y_end or 1815, source_json),
                )
                inserted += 1

    print(
        f"\n[{format_elapsed(start_time)}] CORRAL indexing complete: "
        f"added {inserted} grouped volumes, updated {updated} in data/navy_logs.db.",
        flush=True,
    )

def _download_one(img_url: str, dest: Path):
    req = urllib.request.Request(img_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        out.write(resp.read())
    return dest.name

def download_corral_logbook(source_path: str, ship_name: str, max_workers: int = 5) -> Path:
    """
    Accepts either a single CEDA URL or a JSON list of part URLs.
    Downloads all parts concurrently into data/raw_logs/<slug>/ with part prefixes
    (p01_, p02_...) so alphabetical sorting preserves chronological page order.
    """
    if source_path.strip().startswith("["):
        urls = json.loads(source_path)
    else:
        urls = [source_path]

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", ship_name).strip("_").lower()
    local_dir = RAW_LOGS_DIR / slug
    local_dir.mkdir(parents=True, exist_ok=True)

    for part_idx, remote_url in enumerate(urls, start=1):
        prefix = f"p{part_idx:02d}_"
        existing_part = list(local_dir.glob(f"{prefix}*.jpg")) + list(local_dir.glob(f"{prefix}*.jpeg"))
        if existing_part:
            print(f"Part {part_idx}/{len(urls)} already downloaded ({len(existing_part)} pages).", flush=True)
            continue

        print(f"Fetching image list for Part {part_idx}/{len(urls)}: {remote_url}...", flush=True)
        links = fetch_links(remote_url)
        jpg_urls = sorted({
            urljoin(remote_url, l).split("#")[0].split("?")[0]
            for l in links
            if l.lower().split("?")[0].endswith((".jpg", ".jpeg"))
        })

        tasks = []
        for img_url in jpg_urls:
            raw_name = Path(unquote(urlparse(img_url).path)).name
            dest = local_dir / f"{prefix}{raw_name}"
            if not dest.exists():
                tasks.append((img_url, dest))

        if tasks:
            done = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_download_one, u, d) for u, d in tasks]
                for future in as_completed(futures):
                    done += 1
                    try:
                        fname = future.result()
                        print(f"  Part {part_idx}/{len(urls)} — Downloaded {done}/{len(tasks)}: {fname}", flush=True)
                    except Exception as e:
                        print(f"  Part {part_idx}/{len(urls)} — Failed download: {e}", flush=True)

    return local_dir

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    crawl_corral(limit=limit)