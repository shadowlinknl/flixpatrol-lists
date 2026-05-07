# FlixPatrol Top 10 → mdblist

Daily scraper that pulls FlixPatrol Top 10 pages (Movies, TV Shows, Kids
Movies, Kids TV Shows), resolves every title to an IMDb ID via TMDb, and
commits plain‑text files into this repository. The files are then served via
`raw.githubusercontent.com` URLs and imported as a **Dynamic List** in
[mdblist](https://mdblist.com/).

Which pages get scraped is fully controlled by you in **`pages.txt`** — one
FlixPatrol Top 10 URL per line. The example file ships with the five major
platforms for the Netherlands; add, remove, or swap lines as you like.

## How it works

1. `scraper.py` reads `pages.txt`, fetches every URL it finds there, and
   parses the four Top 10 sections (Movies, TV Shows, Kids Movies, Kids TV
   Shows).
2. For every new title it fetches the FlixPatrol title page once to read the
   release year and media type from JSON‑LD, then queries TMDb to resolve the
   IMDb ID. Results are cached in `cache.json` so subsequent runs only do
   work for new titles.
3. Outputs are written to `lists/`, one IMDb ID per line. Example files:
   - `lists/netflix-movies.txt`
   - `lists/netflix-tv.txt`
   - `lists/netflix-kids-movies.txt`
   - `lists/netflix-kids-tv.txt`
   - `lists/netflix-all.txt` (movies + tv combined, no kids)
   - `lists/netflix-movies-titles.txt` (`Title (Year)` lines, useful as a
     fallback if a TMDb match is missing)
4. A GitHub Actions workflow runs the scraper daily at 05:00 UTC and commits
   any changes back to `main`.

## Setup

### 1. Get a TMDb API key (free)

1. Sign up at <https://www.themoviedb.org/signup>.
2. Open <https://www.themoviedb.org/settings/api> and request an API key.
   Choose “Developer”, fill in the form (any small personal project works).
3. Copy the **API Read Access Token** — that string is what you paste into the
   GitHub secret in step 3.

### 2. Create the GitHub repository

1. Create a new **public** repository on GitHub (mdblist needs to read the raw
   files, so the repo must be public, or hosted somewhere else with a public
   URL). Name it for example `flixpatrol-lists`.
2. Drop the contents of this folder into it. The structure should be:

   ```
   flixpatrol-lists/
   ├── .github/workflows/scrape.yml
   ├── .gitignore
   ├── README.md
   ├── pages.txt           ← edit this to choose which pages get scraped
   ├── requirements.txt
   └── scraper.py
   ```

3. Edit `pages.txt` to your liking (or leave the defaults). Commit & push to
   `main`.

### 3. Add the TMDb key as a repo secret

1. In the repo on GitHub: **Settings → Secrets and variables → Actions →
   New repository secret**.
2. Name: `TMDB_API_KEY` — Value: the key you got in step 1. Save.

### 4. Allow Actions to push commits

1. **Settings → Actions → General → Workflow permissions**.
2. Select **Read and write permissions**, save.

### 5. Trigger the first run manually

1. Go to **Actions → Update FlixPatrol lists → Run workflow** (use the
   `main` branch).
2. Wait for the run to finish (≈ 2–5 minutes for the first run while it
   resolves all titles; later runs are much faster thanks to the cache).
3. Refresh the repo — you should now see a `lists/` folder and a
   `cache.json` file.

### 6. Wire up the URLs in mdblist

The raw URL of any list file looks like this:

```
https://raw.githubusercontent.com/<your-user>/<your-repo>/main/lists/<file>.txt
```

For example:

```
https://raw.githubusercontent.com/Matthias/flixpatrol-lists/main/lists/netflix-movies.txt
https://raw.githubusercontent.com/Matthias/flixpatrol-lists/main/lists/netflix-tv.txt
https://raw.githubusercontent.com/Matthias/flixpatrol-lists/main/lists/disney-all.txt
```

In mdblist:

1. Sign in at <https://mdblist.com/>.
2. Open **My Lists → Create List** (or open an existing list and choose
   **Edit**).
3. Pick **Dynamic List** and paste the raw URL into the **URL** field. mdblist
   will fetch the file, recognise the IMDb IDs (`tt0000000` per line) and
   build the list.
4. Set the refresh interval to whatever you prefer (e.g. every 24 h).

mdblist accepts plain text with one IMDb ID per line — that is exactly what
this scraper produces, so no extra formatting is required.

## Customising — `pages.txt`

`pages.txt` controls everything. One line per FlixPatrol Top 10 page, two
accepted forms:

```text
# name=URL  ← recommended; "name" is the filename prefix in lists/
netflix     = https://flixpatrol.com/top10/netflix/netherlands/
disney      = https://flixpatrol.com/top10/disney/netherlands/

# Just a URL also works; the name is then derived from the URL path,
# e.g. "hbo-belgium" for the line below.
https://flixpatrol.com/top10/hbo/belgium/
```

Rules:

- Lines starting with `#` and blank lines are ignored.
- Names may contain letters, digits, `_`, `-`, `.` (used as filename prefix).
- Duplicate names are skipped with a warning.
- URLs **must** be FlixPatrol Top 10 pages (`/top10/<platform>/<country>/`)
  — that’s the page layout the parser understands.

**Finding URLs:** browse <https://flixpatrol.com/top10/> and copy the URL
of any platform/country page. Common platform slugs include `netflix`,
`disney`, `hbo`, `amazon-prime`, `apple-tv`, `paramount-plus`. Common
country slugs: `netherlands`, `belgium`, `germany`, `united-states`,
`united-kingdom`, …

**To add or remove a platform/country**, just edit `pages.txt`, commit, and
push. The next scheduled run (or a manual one via Actions → Run workflow)
will pick up the new config.

## Running locally

```bash
pip install -r requirements.txt
export TMDB_API_KEY=...your-key...
python scraper.py
```

Without `TMDB_API_KEY` the scraper still produces the `*-titles.txt` files
(one `Title (Year)` per line), but the IMDb ID files will be empty.

## Notes & caveats

- **Cache.** `cache.json` is committed so the bot doesn’t have to re‑resolve
  every title every day. If a title is mis‑matched you can fix it by hand in
  `cache.json` (or just delete that entry to force a re‑lookup).
- **Rate limit.** The scraper sleeps 1 s between FlixPatrol requests and
  retries with exponential back‑off on transient errors. Be courteous and
  don’t lower this aggressively.
- **TOS.** FlixPatrol does not publish a formal API. This is a personal
  scraper for personal use. If FlixPatrol ever asks you to stop, do.
- **Title disambiguation.** TMDb is queried with title + year. The first
  search result is used. For very generic titles you may occasionally get a
  wrong match — the easiest fix is to override that slug’s entry in
  `cache.json`.
# flixpatrol-lists
