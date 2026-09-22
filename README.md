# LinkedIn Profile Posts Scraper

Fetches the latest posts from LinkedIn profiles and company pages using
[Apify's LinkedIn Profile Posts Scraper (No Cookies)](https://apify.com/harvestapi/linkedin-profile-posts).

No LinkedIn login or cookies needed.

---

## Setup

```bash
pip install -r requirements.txt
```

Set your Apify API token (get it from https://console.apify.com/settings/integrations):

```bash
export APIFY_API_TOKEN="your_token_here"
```

---

## Usage

### Single profile

```bash
python scraper.py https://www.linkedin.com/in/satyanadella/
```

### Multiple profiles

```bash
python scraper.py https://www.linkedin.com/in/satyanadella/ https://www.linkedin.com/company/google
```

### From a file (one URL per line)

```bash
python scraper.py --profiles-file profiles.txt
```

### Control how many posts per profile

```bash
python scraper.py https://www.linkedin.com/in/satyanadella/ --max-posts 10
```

### Save raw results as JSON

```bash
python scraper.py https://www.linkedin.com/in/satyanadella/ --output results.json
```

### All options

```
positional arguments:
  profiles              LinkedIn profile/company URLs

options:
  --token TOKEN         Apify API token (or set APIFY_API_TOKEN env var)
  --max-posts N         Max posts per profile (default: 5)
  --output FILE         Save raw results as JSON
  --profiles-file FILE  Text file with one LinkedIn URL per line
```

---

## Supported URL formats

- Personal profiles: `https://www.linkedin.com/in/<username>/`
- Company pages:     `https://www.linkedin.com/company/<name>`
- Direct post URLs:  `https://www.linkedin.com/posts/...`

---

## Pricing

The Apify actor costs **~$1.50 per 1,000 posts**. A free Apify account
includes $5 in monthly credits, which covers ~3,000 posts.

---

## Web app

There's also a Flask web UI (`app.py`) with profile + keyword scraping,
Excel/Google Sheet input & output, and a login screen.

### Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
python app.py          # http://localhost:5000
```

Login credentials come from `APP_USERNAME` / `APP_PASSWORD`. There is no
production default — the app refuses to boot on Render without `APP_PASSWORD`,
so set both as dashboard secrets. Locally, an unset `APP_PASSWORD` falls back to
`dev-only-password`.

### Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `APIFY_API_TOKEN` | yes | Apify token |
| `SECRET_KEY` | yes | Signs session cookies (Render auto-generates) |
| `APP_USERNAME` / `APP_PASSWORD` | yes | Login creds. Set in the Render dashboard; never commit them |
| `GOOGLE_CREDENTIALS_JSON` | for Sheets | Full service-account JSON on one line |
| `PERMANENT_SHEET_URL` | no | Override the default permanent sheet |

For Google Sheets, share the target sheet with the service-account email
(`client_email` in the credentials JSON).

### Deploy to Render

The repo includes `render.yaml` and a `Procfile`. On Render:

1. New → **Web Service** → connect this repo (Render auto-detects `render.yaml`).
2. Set the secret env vars in the dashboard: `APIFY_API_TOKEN`,
   `GOOGLE_CREDENTIALS_JSON` (the entire service-account JSON), `APP_USERNAME`
   and `APP_PASSWORD`. `SECRET_KEY` is generated automatically.
3. Deploy. Health check is at `/health`.

The app is served by gunicorn (`gthread` worker, 600s timeout) so the
streaming scrape runs aren't cut off. Frontend and API share one origin, so
there are no CORS concerns.

**`--workers 1` is load-bearing.** The background job registry (`jobs.py`) lives
in process memory, so a second worker would not see running sweeps.

---

## Keyword sweeps

The keyword actor's `limit` is a **global** result cap for the whole run, and it
works through the keyword list **sequentially**. Sending every keyword in one
call therefore spends the entire budget on keyword #1 — which is why only one
keyword was ever searched. The app now issues **one actor run per keyword**,
fanned out 8 at a time.

A full 295-keyword sweep takes **about an hour** (measured: 75-135s per keyword
run, 12 running concurrently), far past the 600s request timeout, so it runs as
a background job:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/sweep` | Start a sweep; returns `{jobId, keywords, profiles, estimatedCost}` |
| `GET /api/jobs/<id>` | Progress: phase, keywords done, posts found, log |
| `POST /api/jobs/<id>/cancel` | Request cancellation |
| `GET /api/budget` | Remaining Apify allowance and the cost of a full sweep |
| `POST /api/repair-archives` | One-off: restore header rows on archive tabs |

Results are written to the sheet as each stage finishes.

> **On Render's free plan, keep the tab open.** The browser poll is what keeps
> the service awake; a free instance with no inbound requests can spin down and
> take the running job with it. There is no resume — a killed sweep must be
> restarted, and stage 1 is the expensive part. Paid Render instances do not
> spin down.

### Cost

Stage 1 is charged **per run**, so cost scales with the *number of keywords*,
not how deep you go (~$0.044 each). Stage 2 is ~$0.0015 per post fetched. A
295-keyword sweep at 10 posts/keyword is roughly **$16.60**.

A pre-flight guard refuses to start a sweep that would consume more than 90% of
the Apify monthly allowance still remaining.

### Output tabs

| Tab | Contents |
|-----|----------|
| `Profile Posts` | Posts from the profiles in the `Profiles` tab |
| `Keyword Posts` | LinkedIn-wide keyword search results |
| `Profile + Keywords` | Profile posts that mention a keyword (whole-word match) |
| `Keyword Report` | Per keyword: results found, kept, status — use it to prune the list |

Each gets a matching `- Archive` tab that accumulates previous runs.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Fixtures under `tests/fixtures/` are real Apify payloads, so the suite needs no
network and spends nothing.
