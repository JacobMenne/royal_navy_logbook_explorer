import re
import sys
import time
import urllib.request
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
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    parser = LinkParser()
    parser.feed(html)
    return parser.links

def extract_years(text: str) -> tuple[int | None, int | None]:
    years = [int(y) for y in re.findall(r"\b(17\d{2}|18\d{2})\b", text)]
    if not years:
        return None, None
    return min(years), max(years)

def clean_folder_name(url: str) -> str:
    parts = [unquote(p) for p in urlparse(url).path.strip("/").split("/") if p]
    label = " - ".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return label.replace("_", " ").strip()

def normalize_dir_url(url: str) -> str:
    return url if url.endswith("/") else url + "/"

def format_elapsed(start_time: float) -> str:
    secs = int(time.time() - start_time)
    return f"{secs // 60:02d}:{secs % 60:02d}"

def crawl_corral(base_url: str = CORRAL_BASE_URL, max_depth: int = 4, limit: int = 30):
    init_db()
    base_url = normalize_dir_url(base_url)
    discovered = []
    stack = [(base_url, 0)]
    visited = set()
    start_time = time.time()

    print(f"Indexing CEDA/CORRAL archive from {base_url} (limit={limit})...", flush=True)

    while stack and len(discovered) < limit:
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
        # CEDA serves image downloads from dap.ceda.ac.uk, so don't require u.startswith(url)
        jpg_links = sorted({u for u in resolved if u.lower().endswith((".jpg", ".jpeg"))})

        if jpg_links:
            folder_label = clean_folder_name(url)
            y_start, y_end = extract_years(folder_label)
            discovered.append({
                "ship_name": folder_label,
                "year_start": y_start or 1756,
                "year_end": y_end or 1815,
                "url": url,
                "page_count": len(jpg_links),
            })
            print(f"[{format_elapsed(start_time)}]   FOUND ({len(discovered)}/{limit}): {folder_label} ({len(jpg_links)} pages)", flush=True)
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
    with get_connection() as conn:
        for item in discovered:
            exists = conn.execute(
                "SELECT id FROM ships WHERE source_path = ? AND source_type = 'raw_scan'",
                (item["url"],),
            ).fetchone()
            if exists:
                continue

            conn.execute(
                """
                INSERT INTO ships (ship_name, ship_class, year_start, year_end, source_type, source_path)
                VALUES (?, ?, ?, ?, 'raw_scan', ?)
                """,
                (
                    item["ship_name"],
                    f"CORRAL ({item['page_count']} pages)",
                    item["year_start"],
                    item["year_end"],
                    item["url"],
                ),
            )
            inserted += 1

    print(f"\n[{format_elapsed(start_time)}] CORRAL indexing complete: added {inserted} handwritten logbooks to data/navy_logs.db.", flush=True)

def download_corral_logbook(remote_url: str, ship_name: str, max_pages: int | None = None) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", ship_name).strip("_").lower()
    local_dir = RAW_LOGS_DIR / slug
    local_dir.mkdir(parents=True, exist_ok=True)

    existing = list(local_dir.glob("*.jpg")) + list(local_dir.glob("*.jpeg"))
    if existing:
        print(f"Using {len(existing)} already-downloaded pages in {local_dir}", flush=True)
        return local_dir

    print(f"Fetching image list from {remote_url}...", flush=True)
    links = fetch_links(remote_url)
    jpg_urls = sorted({
        urljoin(remote_url, l).split("#")[0].split("?")[0]
        for l in links
        if l.lower().split("?")[0].endswith((".jpg", ".jpeg"))
    })
    if max_pages:
        jpg_urls = jpg_urls[:max_pages]

    for idx, img_url in enumerate(jpg_urls, start=1):
        filename = Path(unquote(urlparse(img_url).path)).name
        dest = local_dir / filename
        print(f"Downloading page {idx}/{len(jpg_urls)}: {filename}...", flush=True)
        req = urllib.request.Request(img_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
            out.write(resp.read())

    return local_dir

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    crawl_corral(limit=limit)