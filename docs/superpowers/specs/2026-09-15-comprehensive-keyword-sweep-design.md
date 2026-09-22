# Comprehensive Keyword Sweep — Design

**Date:** 2026-09-15
**Status:** Approved
**Affects:** `app.py`, new `jobs.py` / `scrapers.py` / `sheets.py`, `templates/index.html`, `static/js/app.js`, `requirements.txt`, `render.yaml`

## Problem

The keyword scraper has searched **1 of 295 keywords** on every run since at least 24 June 2026.

Evidence from the live Google Sheet (`Keyword Posts - Archive`, 127 rows across five runs on
2026-06-24, 06-25, 07-16, 08-07, 08-18): **two distinct keywords** appear in the entire archive —
126 rows from `"University accommodation partnership"` (keyword #1) and 1 row from keyword #2.

### Root cause

A semantic mismatch between the UI and the Apify actor:

1. `templates/index.html` offers **"20 per keyword"**.
2. `app.py` sends a single actor call: `{"keywords": [all 295], "limit": "20"}`.
3. The actor's input schema defines `limit` as *"The number of results, 0 means unlimited"* — a
   **global** cap across the whole run, not per keyword.
4. The actor processes keywords **sequentially**, spending the entire budget on keyword #1 before
   advancing.

Confirmed experimentally (run `uUMyc5cICalg3ctRf`, 2026-09-10): 3 keywords sent with `limit: "9"`
returned **20 items, all from keyword #1**; keywords #2 and #3 returned zero. The actor also
overshoots the limit — it fetches a full page of ~20 regardless.

The 24 June run corroborates the sequential model: `limit=50` produced 47 rows = 46 from keyword #1
plus 1 from keyword #2, i.e. it advanced only once keyword #1 was exhausted.

## Constraints

| Constraint | Value | Source |
|---|---|---|
| Apify plan cap | **$29/month hard cap** | `/v2/users/me/limits` |
| Spent this month | $7.15 (shared with a Facebook Groups project on the same token) | same |
| Max concurrent actor jobs | 32 | same |
| Apify data retention | 31 days | same |
| Request timeout | 600s (gunicorn `--timeout 600`) | `Procfile` |
| Cost per keyword | **~$0.044, fixed** | measured: Aug-18 run, 20 results, $0.044 |
| Per-run cost cap floor | $0.70 — `maxItems` below this is rejected | API error `max-total-charge-usd-below-minimum` |

**Cost is driven by keyword count, not depth.** A full sweep at 10 posts/keyword:
stage 1 = 295 × $0.044 ≈ **$13.00**; stage 2 = ~2,400 unique posts × $0.0015 ≈ **$3.60**;
total ≈ **$16.60**. This fits the remaining budget roughly **once per month**.

## Decisions

| Decision | Choice |
|---|---|
| Posts per keyword | **10** |
| Execution model | **Background job + progress polling** |
| Keyword list | **Run all 295; flag the duds** for evidence-based pruning later |
| Scope | All of issues #1–#5 plus housekeeping |

## Architecture

A 15-minute sweep cannot run inside a 600s request, so the work detaches into a background thread
and the browser polls for progress.

Alternatives considered: a **Render background worker** (proper isolation, but a paid service type
and extra infra for a monthly job) and **client-driven chunking** (no thread, but stalls the moment
the tab closes). The in-process thread needs no new infrastructure and survives a closed tab;
incremental sheet writes cover its weakness of losing state on a worker restart.

> **`--workers 1` is load-bearing.** The job registry lives in process memory. A second gunicorn
> worker would not see jobs created by the first. This is documented at the registry definition.

### Module split

`app.py` is 1,083 lines doing routes, scraping, cleaning and sheet I/O. Adding a job engine to that
makes it unmaintainable.

| Module | Responsibility |
|---|---|
| `jobs.py` | Job registry, thread lifecycle, progress state, cancellation |
| `scrapers.py` | Actor calls, keyword fan-out, dedupe, post cleaning, budget guard |
| `sheets.py` | Google Sheet read/write, archive logic, header repair |
| `app.py` | Routes only — thin controllers |

### Keyword sweep

One actor run **per keyword**, fanned out through a `ThreadPoolExecutor` at **12 concurrent** — under
the 32 cap, leaving headroom for the Facebook project sharing the token. Measured throughput is
~12 keywords per 130s, so a 295-keyword sweep takes **about an hour**.

- Each run gets a 300s `run_timeout` — the actor's own minimum; it fails fast with
  "Low timeout! 300 sec is the minimum." below that (caught by the smoke test).
- Results are truncated to 10 per keyword **after** retrieval. The actor floors `limit` at 20 and
  returns a full page regardless, so stage-1 cost is the same at any depth (confirmed live).
- Post URLs are normalised and deduped **across the whole sweep before stage 2**, so the same post is
  never paid for twice. Where several keywords find the same post, the extras are preserved in an
  `alsoMatchedKeywords` column rather than discarded.
- A keyword that fails is recorded and the sweep continues.

### Status handling

Replace `== "FAILED"` with an explicit success check: anything that is not `SUCCEEDED` — including
`TIMED-OUT`, `ABORTED`, `ABORTING` — is a failure. Stage 2 gains the same check, which it currently
lacks entirely.

### Budget guard

Pre-flight against `/v2/users/me/limits`: if the estimated sweep cost exceeds 90% of the remaining
monthly allowance, the job refuses to start and returns the numbers so the UI can say
*"this run needs $16.60, you have $21.85 left this month."* Each individual actor run also carries a
`max_total_charge_usd` cap.

### Progress API

- `POST /api/jobs` — start, returns `{job_id}` immediately
- `GET /api/jobs/<id>` — `{phase, keywords_done, keywords_total, posts_found, cost_so_far, failures}`
- `POST /api/jobs/<id>/cancel`

## Bundled fixes

1. **Keyword coverage** — the architecture above.
2. **Archive headers** — `archive_has_data = len(get_all_values()) > 0` counts a blank row as data,
   so the header branch never runs. Both live archive tabs currently start with a blank row 1 and
   have no header at all (644 unlabelled rows). Fix the emptiness test and add a one-time repair that
   inserts the correct header.
3. **Combo matcher** — `k in text` becomes a word-boundary regex. Measured on the real 185-post run:
   `"cas"` matched 21 posts, **20 of them inside "Newcastle" / "case" / "showcase"**. Of 150 tags
   written, 26 were wrong and 10 posts were included purely on noise.
4. **Dedupe** — on sweep and on append. The archive currently holds 22 duplicate post URLs of 127.
5. **Profile Posts export** — `run_permanent`'s docstring claims three output tabs but only exports
   two; profile posts are streamed to the browser and lost. Add the tab and its archive.
6. **`Keyword Report` tab** — per keyword: results found, unique kept, status, error, last run date.
   This is the evidence base for pruning the list.
7. **Housekeeping** — pin `requirements.txt` (currently `apify-client>=1.8.1`; v1→v3 changed
   `.call()` from dict to object and the `hasattr` fallbacks silently disable failure detection on
   v1); correct the UI's "per keyword" labels; fix the `render.yaml` service name
   (`linkedin-post-scraper` vs the live `linkedin-apify`).
8. **Credentials** — `APP_PASSWORD` is committed in plaintext in a public repo. Move to a Render
   dashboard secret (`sync: false`) and scrub from the README.

## Testing

No test framework exists today. Add `pytest` over the pure logic, using real Apify payloads captured
from live runs as fixtures so tests need no live calls and spend nothing:

- URL normalisation and cross-keyword dedupe
- Word-boundary matcher — including the real `cas` / "Newcastle" regression
- Budget estimator
- Archive emptiness detection (blank row → header written)
- `clean_post` against a real 185-post payload

Then one live smoke run on 3 keywords (~$0.13) before any full sweep.

## Risks

- A full sweep is a **one-per-month** operation at the current plan and list length.
- On Render's free plan the browser poll is what keeps the instance awake; closing the tab risks
  a spin-down killing the run. There is no resume — a killed sweep restarts from scratch.
- The first sweep adds ~2,400 rows to the sheet.
- Job state is lost if the Render worker restarts mid-sweep; incremental sheet writes mean completed
  keywords survive, but the run must be restarted.
