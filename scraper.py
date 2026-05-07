#!/usr/bin/env python3
"""
FlixPatrol Top 10 -> IMDb ID scraper for mdblist dynamic lists.

Fetches the daily Top 10 (Movies, TV Shows, Kids Movies, Kids TV Shows) for
each FlixPatrol page listed in `pages.txt`, resolves every title to an IMDb
ID via TMDb, and writes plain-text files with one IMDb ID per line. The text
files can be served via the GitHub raw URL and imported into mdblist as a
dynamic list.

Environment variables:
    TMDB_API_KEY    Required to resolve titles -> IMDb IDs (free at
                    https://www.themoviedb.org/settings/api). If not set, the
                    scraper still writes <name>-<section>-titles.txt files
                    with "Title (Year)" lines.

Configuration: edit `pages.txt` next to this script.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import cloudscraper
import requests


# --- Configuration ---------------------------------------------------------

# Pages to scrape are configured in pages.txt (one URL per line).
PAGES_FILE = "pages.txt"

# Sections to scrape from each FlixPatrol page.
# Mapping: section header on page -> output filename suffix + default media type
SECTIONS: dict[str, tuple[str, str]] = {
    "TOP 10 Movies":        ("movies",     "movie"),
    "TOP 10 TV Shows":      ("tv",         "tv"),
    "TOP 10 Kids Movies":   ("kids-movies", "movie"),
    "TOP 10 Kids TV Shows": ("kids-tv",    "tv"),
}

# Limit to a subset of the sections above.
# Set to `None` to scrape all four sections.
# Set to e.g. {"TOP 10 TV Shows"} to scrape only adult TV series.
SECTIONS_ENABLED: set[str] | None = {"TOP 10 TV Shows"}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent
LISTS_DIR = ROOT / "lists"
CACHE_FILE = ROOT / "cache.json"
PAGES_PATH = ROOT / PAGES_FILE

REQUEST_DELAY_SECONDS = 1.0      # polite delay between FlixPatrol requests
HTTP_TIMEOUT = 30
MAX_HTTP_RETRIES = 3

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"


# --- HTTP helpers ----------------------------------------------------------

# FlixPatrol sits behind Cloudflare and 403s plain `requests` traffic from cloud
# IPs (incl. GitHub Actions runners). cloudscraper emulates a browser closely
# enough to pass Cloudflare's JS challenge in most cases.
flix_session = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)
flix_session.headers.update({
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# TMDb is a normal API, so plain requests is fine (and faster).
# Accept either v3 API key (32-char hex) or v4 Read Access Token (JWT, starts
# with "eyJ"). The Bearer header path is used for the JWT.
TMDB_USE_BEARER = TMDB_API_KEY.startswith("eyJ")
tmdb_session = requests.Session()
tmdb_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
if TMDB_USE_BEARER:
    tmdb_session.headers["Authorization"] = f"Bearer {TMDB_API_KEY}"


def _tmdb_get(path: str, params: dict | None = None):
    p = dict(params or {})
    if not TMDB_USE_BEARER and TMDB_API_KEY:
        p["api_key"] = TMDB_API_KEY
    return tmdb_session.get(f"{TMDB_BASE}{path}", params=p, timeout=HTTP_TIMEOUT)


def http_get(url: str, *, params: dict | None = None) -> Optional[str]:
    """GET FlixPatrol with simple retry/backoff. Returns response text or None."""
    backoff = 2
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            r = flix_session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.text
            print(f"    HTTP {r.status_code} ({attempt}/{MAX_HTTP_RETRIES}) for {url}",
                  file=sys.stderr)
        except requests.RequestException as exc:
            print(f"    Error ({attempt}/{MAX_HTTP_RETRIES}) for {url}: {exc}",
                  file=sys.stderr)
        time.sleep(backoff)
        backoff *= 2
    return None


# --- FlixPatrol parsing ----------------------------------------------------

TITLE_LINK_RE = re.compile(
    r'<a[^>]+href="(/title/([^"/]+)/)"[^>]*>([^<]+)</a>'
)
JSONLD_TYPE_RE = re.compile(r'"@type"\s*:\s*"(Movie|TVSeries)"')
JSONLD_DATE_RE = re.compile(r'"dateCreated"\s*:\s*"(\d{4})')


def parse_top10(page_html: str) -> dict[str, list[tuple[str, str]]]:
    """
    Parse a FlixPatrol /top10/<platform>/<country>/ page.
    Returns { section_label: [ (slug, display_title), ... ] }.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    section_starts = []
    for label in SECTIONS:
        idx = page_html.find(label)
        if idx != -1:
            section_starts.append((idx, label))
    section_starts.sort()

    for i, (start, label) in enumerate(section_starts):
        end = section_starts[i + 1][0] if i + 1 < len(section_starts) else len(page_html)
        chunk = page_html[start:end]

        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in TITLE_LINK_RE.finditer(chunk):
            slug = m.group(2)
            if slug in seen:
                continue
            seen.add(slug)
            title = html_lib.unescape(m.group(3)).strip()
            items.append((slug, title))
            if len(items) >= 10:
                break
        out[label] = items
    return out


def parse_title_page(page_html: str) -> tuple[Optional[int], Optional[str]]:
    """Return (year, media_type) extracted from JSON-LD on a title page."""
    year = None
    if (m := JSONLD_DATE_RE.search(page_html)):
        try:
            year = int(m.group(1))
        except ValueError:
            year = None

    media_type = None
    if (m := JSONLD_TYPE_RE.search(page_html)):
        media_type = "movie" if m.group(1) == "Movie" else "tv"
    return year, media_type


# --- TMDb lookup -----------------------------------------------------------

def tmdb_imdb_id(title: str, year: Optional[int], media_type: str) -> Optional[str]:
    """Search TMDb by title (and year, if known) and return its IMDb ID."""
    if not TMDB_API_KEY:
        return None

    is_movie = media_type == "movie"
    search_path = f"/search/{'movie' if is_movie else 'tv'}"
    params = {"query": title, "include_adult": "false"}
    if year:
        params["year" if is_movie else "first_air_date_year"] = year

    try:
        r = _tmdb_get(search_path, params)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results and year:
            # Retry without year in case FlixPatrol's date is off
            params.pop("year" if is_movie else "first_air_date_year", None)
            r = _tmdb_get(search_path, params)
            r.raise_for_status()
            results = r.json().get("results", [])
        if not results:
            return None

        tmdb_id = results[0]["id"]
        if is_movie:
            details = _tmdb_get(f"/movie/{tmdb_id}").json()
            return details.get("imdb_id") or None
        else:
            ext = _tmdb_get(f"/tv/{tmdb_id}/external_ids").json()
            return ext.get("imdb_id") or None
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"    TMDb lookup error for {title!r}: {exc}", file=sys.stderr)
        return None


# --- Pages config loader ---------------------------------------------------

def _derive_name_from_url(url: str) -> str:
    """Fall back name when a line has no `name=`. /top10/netflix/netherlands/ -> netflix-netherlands"""
    parts = [p for p in url.rstrip("/").split("/") if p and "://" not in p and p != "flixpatrol.com"]
    tail = parts[-2:] if len(parts) >= 2 else parts[-1:]
    return "-".join(tail) or "page"


def load_pages() -> list[tuple[str, str]]:
    """Read pages.txt: returns list of (output_prefix, url)."""
    if not PAGES_PATH.exists():
        sys.exit(
            f"ERROR: {PAGES_PATH.name} not found next to scraper.py. "
            "Create it with one FlixPatrol Top 10 URL per line "
            "(see the example pages.txt in the repo)."
        )

    pages: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for lineno, raw in enumerate(PAGES_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, _, url = line.partition("=")
            name, url = name.strip(), url.strip()
        else:
            url = line
            name = _derive_name_from_url(url)

        if not url.startswith(("http://", "https://")):
            print(f"  WARN {PAGES_PATH.name}:{lineno}: ignoring (not a URL): {line!r}",
                  file=sys.stderr)
            continue
        if not re.match(r"^[A-Za-z0-9._-]+$", name):
            print(f"  WARN {PAGES_PATH.name}:{lineno}: skipping invalid name {name!r}",
                  file=sys.stderr)
            continue
        if name in seen_names:
            print(f"  WARN {PAGES_PATH.name}:{lineno}: duplicate name {name!r}, skipping",
                  file=sys.stderr)
            continue
        seen_names.add(name)
        pages.append((name, url))

    if not pages:
        sys.exit(f"ERROR: {PAGES_PATH.name} contains no usable entries.")
    return pages


# --- Cache helpers ---------------------------------------------------------

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: cache.json is malformed, starting fresh.", file=sys.stderr)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# --- Output writers --------------------------------------------------------

def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# --- Main ------------------------------------------------------------------

def main() -> int:
    if not TMDB_API_KEY:
        print(
            "WARNING: TMDB_API_KEY is not set. "
            "Will write *-titles.txt files only (mdblist will need IMDb IDs for best results).",
            file=sys.stderr,
        )

    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    pages = load_pages()
    print(f"Loaded {len(pages)} page(s) from {PAGES_PATH.name}")

    for platform_key, url in pages:
        print(f"\n=== {platform_key} ===")
        print(f"GET {url}")
        page = http_get(url)
        if not page:
            print(f"  Skipping {platform_key}: failed to fetch page")
            continue
        time.sleep(REQUEST_DELAY_SECONDS)

        sections = parse_top10(page)
        if not any(sections.values()):
            print(f"  Skipping {platform_key}: no top10 sections parsed")
            continue

        all_imdb: list[str] = []
        all_titles: list[str] = []

        for section_label, (suffix, default_type) in SECTIONS.items():
            if SECTIONS_ENABLED is not None and section_label not in SECTIONS_ENABLED:
                continue
            items = sections.get(section_label, [])
            if not items:
                continue

            imdb_lines: list[str] = []
            title_lines: list[str] = []

            for slug, display_title in items:
                entry = cache.get(slug)

                # Step 1: resolve year if not yet cached. Type ALWAYS comes from
                # the FlixPatrol section, because their JSON-LD marks everything
                # as "Movie" — including TV series — which makes JSON-LD's
                # @type useless for our purposes.
                if not entry or "year" not in entry:
                    print(f"  fetch title page: {display_title}  ({slug})")
                    title_html = http_get(f"https://flixpatrol.com/title/{slug}/")
                    time.sleep(REQUEST_DELAY_SECONDS)
                    year, _ = parse_title_page(title_html or "")
                    entry = entry or {}
                    entry.update({
                        "title": display_title,
                        "year":  year,
                        "type":  default_type,
                    })
                    cache[slug] = entry
                    save_cache(cache)

                # Self-heal: cache made before the type-from-section fix may
                # have the wrong type. If the cached type doesn't match the
                # section, override it and clear imdb_id to force a re-lookup.
                if entry.get("type") != default_type:
                    print(f"  type fix: {slug}: {entry.get('type')!r} -> {default_type!r}")
                    entry["type"] = default_type
                    entry.pop("imdb_id", None)
                    cache[slug] = entry
                    save_cache(cache)

                # Step 2: resolve IMDb ID if missing (retry every run until found)
                if not entry.get("imdb_id"):
                    imdb_id = tmdb_imdb_id(
                        entry.get("title", display_title),
                        entry.get("year"),
                        entry.get("type", default_type),
                    )
                    if imdb_id:
                        entry["imdb_id"] = imdb_id
                        cache[slug] = entry
                        save_cache(cache)

                if entry.get("imdb_id"):
                    imdb_lines.append(entry["imdb_id"])
                    all_imdb.append(entry["imdb_id"])

                title_line = entry.get("title", display_title)
                if entry.get("year"):
                    title_line = f"{title_line} ({entry['year']})"
                title_lines.append(title_line)
                all_titles.append(title_line)

            write_lines(LISTS_DIR / f"{platform_key}-{suffix}.txt", imdb_lines)
            write_lines(LISTS_DIR / f"{platform_key}-{suffix}-titles.txt", title_lines)
            print(f"  {section_label}: {len(imdb_lines)}/{len(items)} IMDb IDs resolved")

        write_lines(LISTS_DIR / f"{platform_key}-all.txt", all_imdb)
        write_lines(LISTS_DIR / f"{platform_key}-all-titles.txt", all_titles)

    save_cache(cache)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
