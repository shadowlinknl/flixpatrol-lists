#!/usr/bin/env python3
"""
push_to_mdblist.py — sync `lists/*.txt` files into mdblist Static Lists.

Reads `mdblist-targets.txt`, takes each line as a mapping from a local list
filename (without `.txt`) to an mdblist list (by ID or `username/slug`), and
makes the mdblist list contain exactly the IMDb IDs in the local file.

It does an additive + subtractive diff so it doesn't churn unchanged items:
    1. GET  /lists/{ref}/items                 -> existing IMDb IDs
    2. POST /lists/{ref}/items/remove          -> drop items no longer present
    3. POST /lists/{ref}/items/add             -> add new items

Movie/TV split is read from cache.json (filled in by scraper.py), so the API
gets the correct shape: {"movies": [...], "shows": [...]}.

Required environment:
    MDBLIST_API_KEY    free key from https://mdblist.com/preferences/

Targets file format (one mapping per line, # = comment):
    netflix-all      12345
    netflix-tv       matthias/netflix-tv-nl

The first column is the prefix of the local file in `lists/`.
The second column is either a numeric mdblist list ID or `username/list-slug`.

Free mdblist tier allows max 4 Static Lists. If you need more, support on
Patreon (1€) bumps you to 10/25/100/250 lists depending on tier.

Exit codes: 0 on success / no work, 1 on configuration error,
2 if any list failed (the rest still ran).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import requests


ROOT = Path(__file__).resolve().parent
LISTS_DIR = ROOT / "lists"
TARGETS_FILE = ROOT / "mdblist-targets.txt"
CACHE_FILE = ROOT / "cache.json"

MDBLIST_BASE = "https://api.mdblist.com"
MDBLIST_API_KEY = os.environ.get("MDBLIST_API_KEY", "").strip()
HTTP_TIMEOUT = 30
REQUEST_DELAY = 0.5  # be polite

session = requests.Session()
session.headers.update({"Accept": "application/json"})


# --- Targets file ----------------------------------------------------------

def load_targets() -> list[tuple[str, str]]:
    """Return [(local_name, list_ref), ...] from mdblist-targets.txt."""
    if not TARGETS_FILE.exists():
        return []
    out: list[tuple[str, str]] = []
    for lineno, raw in enumerate(TARGETS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            print(f"  WARN {TARGETS_FILE.name}:{lineno}: bad line, expected '<local-name> <list-ref>': {line!r}",
                  file=sys.stderr)
            continue
        out.append((parts[0], parts[1]))
    return out


# --- IMDb -> media type lookup --------------------------------------------

def build_type_index(cache: dict) -> dict[str, str]:
    """imdb_id -> 'movie' or 'tv', built from cache.json entries."""
    idx: dict[str, str] = {}
    for entry in cache.values():
        imdb_id = entry.get("imdb_id")
        media_type = entry.get("type")
        if imdb_id and media_type in ("movie", "tv"):
            idx[imdb_id] = media_type
    return idx


def split_by_type(imdb_ids: Iterable[str], type_index: dict[str, str]) -> dict[str, list[dict]]:
    """Group IMDb IDs into mdblist's expected {'movies': [...], 'shows': [...]} shape."""
    movies: list[dict] = []
    shows: list[dict] = []
    unknown: list[str] = []
    for imdb_id in imdb_ids:
        media_type = type_index.get(imdb_id)
        if media_type == "movie":
            movies.append({"imdb": imdb_id})
        elif media_type == "tv":
            shows.append({"imdb": imdb_id})
        else:
            # If we don't know the type, default to movie. mdblist will return
            # not_found for the wrong bucket, and we'll surface a warning.
            unknown.append(imdb_id)
            movies.append({"imdb": imdb_id})
    if unknown:
        print(f"    NOTE: {len(unknown)} IMDb ID(s) had no cached type, sent as movies",
              file=sys.stderr)
    return {"movies": movies, "shows": shows}


# --- mdblist API helpers ---------------------------------------------------

# username/slug -> numeric id, populated lazily.
_id_cache: dict[str, str] = {}


def resolve_list_id(list_ref: str) -> Optional[str]:
    """
    Translate `username/slug` to the numeric mdblist list ID.
    Numeric refs are returned unchanged.

    The Modify Static List Items endpoint (`POST /lists/{id}/items/{action}`)
    only accepts the numeric ID, so we resolve once and reuse.
    """
    if list_ref.isdigit():
        return list_ref
    if list_ref in _id_cache:
        return _id_cache[list_ref]

    url = f"{MDBLIST_BASE}/lists/{list_ref}"
    try:
        r = session.get(url, params={"apikey": MDBLIST_API_KEY}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    ERROR resolving list {list_ref}: {exc}", file=sys.stderr)
        return None

    # The API has historically returned this as a single dict OR a
    # 1-element list. Handle both.
    candidate = None
    if isinstance(data, dict):
        candidate = data
    elif isinstance(data, list) and data:
        candidate = data[0]
    if isinstance(candidate, dict) and candidate.get("id"):
        list_id = str(candidate["id"])
        _id_cache[list_ref] = list_id
        return list_id

    print(f"    ERROR: could not find numeric ID in response for {list_ref}: "
          f"{str(data)[:200]}", file=sys.stderr)
    return None


def get_existing_imdb_ids(list_id: str) -> Optional[set[str]]:
    """Fetch all IMDb IDs currently in the mdblist list (numeric ID only)."""
    url = f"{MDBLIST_BASE}/lists/{list_id}/items"
    try:
        r = session.get(url, params={"apikey": MDBLIST_API_KEY, "limit": 1000},
                        timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    ERROR reading mdblist list {list_id}: {exc}", file=sys.stderr)
        return None

    ids: set[str] = set()
    if isinstance(data, dict):
        for key in ("movies", "shows"):
            for item in data.get(key) or []:
                imdb = item.get("imdb_id") or item.get("imdb")
                if imdb:
                    ids.add(imdb)
    elif isinstance(data, list):
        for item in data:
            imdb = item.get("imdb_id") or item.get("imdb")
            if imdb:
                ids.add(imdb)
    return ids


def modify_items(list_id: str, action: str, payload: dict) -> Optional[dict]:
    """POST /lists/{listid}/items/{action} where action is 'add' or 'remove'."""
    if not (payload.get("movies") or payload.get("shows")):
        return {"skipped": True}
    url = f"{MDBLIST_BASE}/lists/{list_id}/items/{action}"
    try:
        r = session.post(
            url,
            params={"apikey": MDBLIST_API_KEY},
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    ERROR {action} on list {list_id}: {exc}", file=sys.stderr)
        return None


# --- Sync logic ------------------------------------------------------------

def sync_one(local_name: str, list_ref: str, type_index: dict[str, str]) -> bool:
    """Sync one local list -> one mdblist list. Returns True on success."""
    local_file = LISTS_DIR / f"{local_name}.txt"
    if not local_file.exists():
        print(f"  SKIP {local_name}: lists/{local_name}.txt not found", file=sys.stderr)
        return False

    desired_ids = {
        line.strip()
        for line in local_file.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("tt")
    }
    if not desired_ids:
        print(f"  SKIP {local_name}: lists/{local_name}.txt has no IMDb IDs", file=sys.stderr)
        return False

    list_id = resolve_list_id(list_ref)
    if not list_id:
        return False

    existing_ids = get_existing_imdb_ids(list_id)
    if existing_ids is None:
        return False

    to_remove = sorted(existing_ids - desired_ids)
    to_add = sorted(desired_ids - existing_ids)
    unchanged = len(existing_ids & desired_ids)

    label = list_ref if list_ref == list_id else f"{list_ref} (#{list_id})"
    print(f"  {local_name} -> {label}: "
          f"{unchanged} unchanged, +{len(to_add)} to add, -{len(to_remove)} to remove")

    success = True

    if to_remove:
        payload = split_by_type(to_remove, type_index)
        result = modify_items(list_id, "remove", payload)
        if result is None:
            success = False
        time.sleep(REQUEST_DELAY)

    if to_add:
        payload = split_by_type(to_add, type_index)
        result = modify_items(list_id, "add", payload)
        if result is None:
            success = False
        else:
            nf = result.get("not_found", {}) if isinstance(result, dict) else {}
            if isinstance(nf, dict) and (nf.get("movies") or nf.get("shows")):
                print(f"    WARN: {nf.get('movies', 0)} movies / {nf.get('shows', 0)} "
                      f"shows not found by mdblist", file=sys.stderr)
        time.sleep(REQUEST_DELAY)

    return success


def main() -> int:
    if not MDBLIST_API_KEY:
        print("MDBLIST_API_KEY not set; skipping mdblist sync.")
        return 0

    targets = load_targets()
    if not targets:
        print(f"{TARGETS_FILE.name} is missing or empty; nothing to sync.")
        return 0

    cache: dict = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: cache.json malformed, type detection may be poor.",
                  file=sys.stderr)
    type_index = build_type_index(cache)
    print(f"Loaded {len(targets)} target(s), {len(type_index)} cached IMDb IDs.\n")

    failures = 0
    for local_name, list_ref in targets:
        ok = sync_one(local_name, list_ref, type_index)
        if not ok:
            failures += 1

    if failures:
        print(f"\nDone with {failures} failure(s).", file=sys.stderr)
        return 2
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
