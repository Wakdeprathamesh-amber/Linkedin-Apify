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

Default login: **Amber** / **Amber@123** (override via `APP_USERNAME` /
`APP_PASSWORD`).

### Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `APIFY_API_TOKEN` | yes | Apify token |
| `SECRET_KEY` | yes | Signs session cookies (Render auto-generates) |
| `APP_USERNAME` / `APP_PASSWORD` | no | Login creds (default Amber / Amber@123) |
| `GOOGLE_CREDENTIALS_JSON` | for Sheets | Full service-account JSON on one line |
| `PERMANENT_SHEET_URL` | no | Override the default permanent sheet |

For Google Sheets, share the target sheet with the service-account email
(`client_email` in the credentials JSON).

### Deploy to Render

The repo includes `render.yaml` and a `Procfile`. On Render:

1. New → **Web Service** → connect this repo (Render auto-detects `render.yaml`).
2. Set the two secret env vars in the dashboard: `APIFY_API_TOKEN` and
   `GOOGLE_CREDENTIALS_JSON` (paste the entire service-account JSON).
   `SECRET_KEY` is generated automatically; `APP_USERNAME` / `APP_PASSWORD`
   default to Amber / Amber@123.
3. Deploy. Health check is at `/health`.

The app is served by gunicorn (`gthread` worker, 600s timeout) so the
streaming scrape runs aren't cut off. Frontend and API share one origin, so
there are no CORS concerns.
