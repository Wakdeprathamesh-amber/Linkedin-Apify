"""
Flask web app for LinkedIn Post Scraper
- Profile posts via harvestapi/linkedin-profile-posts
- Keyword posts via sasky/linkedin-keyword-posts-urls-scraper
- Google Sheets as input source and output destination
"""

import os
import json
import io
from datetime import datetime
from flask import (
    Flask, render_template, request, jsonify, Response, send_file,
    session, redirect, url_for,
)
from apify_client import ApifyClient
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook

import jobs
import scrapers
import sheets
from scrapers import (
    PROFILE_ACTOR, clean_keywords, clean_post, compile_keyword_patterns,
    get_dataset_id, group_posts, match_keywords,
)
from sheets import export_to_sheet, read_sheet_column

load_dotenv(override=True)

app = Flask(__name__)
# Session secret — MUST be set via env in production (Render env var SECRET_KEY)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")

PERMANENT_SHEET_URL = os.environ.get(
    "PERMANENT_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/18IbGRZ-aJWI2QVxjHS1zZBo8o-IKrndsi4zu-w7jT4Q",
)
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")

# ─── Login credentials ───────────────────────────────────────────────────────
# Never hard-code these: this repo is public. Set them as Render dashboard
# secrets. A deploy without APP_PASSWORD fails fast rather than falling back to
# a published default.
APP_USERNAME = os.environ.get("APP_USERNAME", "Amber")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

if not APP_PASSWORD:
    if os.environ.get("RENDER"):
        raise RuntimeError(
            "APP_PASSWORD is not set. Add it as an environment variable in the "
            "Render dashboard (Settings -> Environment) before deploying."
        )
    APP_PASSWORD = "dev-only-password"  # local development only

# ─── Google Sheets credentials ───────────────────────────────────────────────
# Preferred for cloud/Render: full service-account JSON in env GOOGLE_CREDENTIALS_JSON.
# Local fallback: a credentials.json file path (env GOOGLE_CREDS_JSON).
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GSHEET_CREDS = os.environ.get("GOOGLE_CREDS_JSON", "credentials.json")


# ─── Auth ────────────────────────────────────────────────────────────────────

# Endpoints reachable without logging in
PUBLIC_ENDPOINTS = {"login", "logout", "health", "static"}


@app.before_request
def require_login():
    """Gate every route behind a session login, except public endpoints."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("logged_in"):
        return None
    # API callers get JSON 401; browser navigations get redirected to login.
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required. Please log in again."}), 401
    return redirect(url_for("login"))


# ─── Shared helpers ─────────────────────────────────────────────────────────
# Sheet I/O lives in sheets.py; Apify calls and post cleaning in scrapers.py.
# Both are unit-tested without Flask (see tests/).

# ─── Keyword sweep ──────────────────────────────────────────────────────────

TAB_PROFILE = "Profile Posts"
TAB_KEYWORD = "Keyword Posts"
TAB_COMBO = "Profile + Keywords"
TAB_REPORT = "Keyword Report"

ARCHIVE_HEADER = [
    "authorName", "postedAt", "text", "postUrl",
    "reactions", "comments", "shares", "keyword", "scrapeDate",
]


def make_rows(posts: list) -> list:
    """Flatten cleaned posts into the sheet's column order."""
    return [{
        "authorName": p.get("authorName", ""),
        "postedAt": p.get("postedAt", ""),
        "text": (p.get("text", ""))[:5000],
        "postUrl": p.get("postUrl", ""),
        "reactions": p.get("reactionsCount", 0),
        "comments": p.get("commentsCount", 0),
        "shares": p.get("sharesCount", 0),
        "keyword": p.get("keyword", ""),
        "alsoMatchedKeywords": p.get("alsoMatchedKeywords", ""),
    } for p in posts]


def run_sweep(job, token, profiles, keywords, max_posts, per_keyword, kw_date, sheet_url):
    """Full sweep: profiles, every keyword, combo, then export.

    Runs on a background thread — see jobs.py. Each stage writes to the sheet as
    soon as it finishes, so a worker restart loses progress but not finished work.
    """
    client = ApifyClient(token)
    job.keywords_total = len(keywords)

    # ── Budget guard ──────────────────────────────────────────────────────
    estimate = scrapers.estimate_sweep_cost(len(keywords), per_keyword)
    job.estimated_cost = estimate
    try:
        budget = scrapers.fetch_budget(token)
        verdict = scrapers.evaluate_budget(estimate, budget["used"], budget["cap"])
        if not verdict["ok"]:
            raise RuntimeError(
                f"Refusing to start: this run needs about ${verdict['estimated']}, "
                f"but only ${verdict['remaining']} of the ${verdict['cap']} monthly "
                f"Apify budget is left."
            )
        job.log(f"budget ok — est ${estimate}, ${verdict['remaining']} remaining this month")
    except RuntimeError:
        raise
    except Exception as exc:
        job.log(f"budget check skipped ({exc})")

    profile_posts_raw, profile_cleaned, keyword_posts, combo_posts = [], [], [], []

    # ── 1. Profile posts ──────────────────────────────────────────────────
    if profiles and not job.should_cancel():
        job.set_phase(f"Scraping {len(profiles)} profiles")
        try:
            run = client.actor(PROFILE_ACTOR).call(run_input={
                "targetUrls": profiles, "maxPosts": max_posts,
                "maxReactions": 0, "postNestedReactions": False,
                "maxComments": 0, "postNestedComments": False,
            })
            if not scrapers.is_successful(
                run.status if hasattr(run, "status") else (run or {}).get("status", "")
            ):
                raise scrapers.ActorError("profile run did not succeed")
            profile_posts_raw = list(client.dataset(get_dataset_id(run)).iterate_items())
            profile_cleaned = [clean_post(p) for p in profile_posts_raw]
            profile_cleaned.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
            job.log(f"{len(profile_cleaned)} profile posts")
        except Exception as exc:
            job.log(f"profile stage failed: {exc}")

    # ── 2. Keyword sweep — one actor run per keyword ──────────────────────
    report = []
    if keywords and not job.should_cancel():
        job.set_phase(f"Searching {len(keywords)} keywords")
        raw_rows, report = scrapers.sweep_keywords(
            client, keywords, kw_date, per_keyword,
            on_progress=lambda done, total, posts: job.progress(done, total, posts),
            should_cancel=job.should_cancel,
        )
        unique, extras = scrapers.dedupe_keyword_results(raw_rows)
        job.log(f"{len(raw_rows)} results, {len(unique)} unique after dedupe")

        # ── 3. Fetch full content for the unique URLs ─────────────────────
        urls = [r["post_url"] for r in unique if r.get("post_url")]
        if urls and not job.should_cancel():
            job.set_phase(f"Fetching content for {len(urls)} posts")
            content = scrapers.fetch_post_content(
                client, urls,
                on_progress=lambda done, total, posts: job.log(
                    f"content batch {done}/{total} — {posts} posts"
                ),
                should_cancel=job.should_cancel,
            )
            url_to_kw = {scrapers.normalize_post_url(r.get("post_url", "")): r.get("keyword", "")
                         for r in unique}
            for post in content:
                cleaned = clean_post(post)
                query = post.get("query") or {}
                target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                key = scrapers.normalize_post_url(target) or scrapers.normalize_post_url(
                    cleaned.get("postUrl", "")
                )
                cleaned["keyword"] = url_to_kw.get(key, "")
                others = [k for k in extras.get(key, []) if k and k != cleaned["keyword"]]
                cleaned["alsoMatchedKeywords"] = ", ".join(others)
                keyword_posts.append(cleaned)
            keyword_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
            job.posts_found = len(keyword_posts)

    # ── 4. Combo — profile posts that mention a keyword (whole words only) ─
    if profile_posts_raw and keywords:
        job.set_phase("Filtering profile posts by keyword")
        patterns = compile_keyword_patterns(keywords)
        for post in profile_posts_raw:
            text = post.get("content") or post.get("text") or post.get("description") or ""
            matched = match_keywords(text, patterns)
            if matched:
                cleaned = clean_post(post)
                cleaned["keyword"] = ", ".join(matched)
                combo_posts.append(cleaned)
        combo_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
        job.log(f"{len(combo_posts)} combo matches")

    # ── 5. Export ─────────────────────────────────────────────────────────
    if sheet_url:
        job.set_phase("Saving to Google Sheet")
        today = datetime.now().strftime("%Y-%m-%d")
        for tab, posts in (
            (TAB_PROFILE, profile_cleaned),
            (TAB_KEYWORD, keyword_posts),
            (TAB_COMBO, combo_posts),
        ):
            if not posts:
                continue
            try:
                export_to_sheet(sheet_url, make_rows(posts), tab)
                job.log(f"wrote {len(posts)} rows to '{tab}'")
            except Exception as exc:
                job.log(f"could not write '{tab}': {exc}")
        if report:
            try:
                sheets.write_tab(sheet_url, sheets.build_keyword_report(report, today), TAB_REPORT)
                duds = sum(1 for r in report if not r.get("found"))
                job.log(f"keyword report written — {duds} of {len(report)} returned nothing")
            except Exception as exc:
                job.log(f"could not write '{TAB_REPORT}': {exc}")

    job.result = {
        "profilePosts": len(profile_cleaned),
        "keywordPosts": len(keyword_posts),
        "comboPosts": len(combo_posts),
        "keywordsSearched": len(report),
        "keywordsWithResults": sum(1 for r in report if r.get("found")),
        "keywordsFailed": sum(1 for r in report if r.get("status") == "failed"),
        "estimatedCost": job.estimated_cost,
    }


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == APP_USERNAME and password == APP_PASSWORD:
            session["logged_in"] = True
            session["user"] = username
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/health")
def health():
    """Unauthenticated health check for Render.

    Reports the deployed commit so a deploy can be confirmed from outside
    without logging in — otherwise a successful health check only proves that
    *some* revision is up, not which one. RENDER_GIT_COMMIT is set by Render.
    """
    return jsonify({
        "status": "ok",
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
    })


@app.route("/")
def index():
    return render_template("index.html", user=session.get("user", ""))


@app.route("/api/budget", methods=["GET"])
def budget():
    """Remaining Apify allowance, and what a full sweep would cost."""
    if not APIFY_TOKEN:
        return jsonify({"error": "Apify API token not configured."}), 500
    try:
        current = scrapers.fetch_budget(APIFY_TOKEN)
        keywords = clean_keywords(read_sheet_column(PERMANENT_SHEET_URL, "Keywords"))
        per_keyword = int(request.args.get("perKeyword", 10))
        estimate = scrapers.estimate_sweep_cost(len(keywords), per_keyword)
        verdict = scrapers.evaluate_budget(estimate, current["used"], current["cap"])
        verdict["keywords"] = len(keywords)
        return jsonify(verdict)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sweep", methods=["POST"])
def start_sweep():
    """Kick off a full sweep in the background and return its job id.

    A 295-keyword sweep runs for ~15 minutes, well past the 600s request
    timeout, so the work detaches and the browser polls /api/jobs/<id>.
    """
    if not APIFY_TOKEN:
        return jsonify({"error": "Apify API token not configured."}), 500

    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheetUrl") or PERMANENT_SHEET_URL
    max_posts = int(data.get("maxPosts", 5))
    per_keyword = max(1, int(data.get("perKeyword", 10)))
    kw_date = data.get("kwDate", "last-1-week")

    profiles = data.get("profiles")
    keywords = data.get("keywords")
    if profiles is None or keywords is None:
        try:
            profiles = read_sheet_column(sheet_url, "Profiles")
            keywords = read_sheet_column(sheet_url, "Keywords")
        except Exception as e:
            return jsonify({"error": f"Failed to read sheet: {str(e)}"}), 500

    profiles = [p.strip() for p in (profiles or []) if p.strip()]
    keywords = clean_keywords(keywords or [])
    if not profiles and not keywords:
        return jsonify({"error": "No profiles or keywords found."}), 400

    job = jobs.create("sweep")
    jobs.start(job, run_sweep, APIFY_TOKEN, profiles, keywords,
               max_posts, per_keyword, kw_date, sheet_url)
    return jsonify({
        "jobId": job.id,
        "keywords": len(keywords),
        "profiles": len(profiles),
        "estimatedCost": scrapers.estimate_sweep_cost(len(keywords), per_keyword),
    })


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job. It may have expired or the server restarted."}), 404
    return jsonify(job.to_dict())


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    if not jobs.cancel(job_id):
        return jsonify({"error": "Job not found or already finished."}), 404
    return jsonify({"success": True})


@app.route("/api/repair-archives", methods=["POST"])
def repair_archives():
    """One-off: put the header row back on archive tabs written without one."""
    data = request.get_json(silent=True) or {}
    sheet_url = data.get("sheetUrl") or PERMANENT_SHEET_URL
    tabs = data.get("tabs") or [
        f"{TAB_KEYWORD} - Archive", f"{TAB_COMBO} - Archive", f"{TAB_PROFILE} - Archive",
    ]
    try:
        return jsonify({"results": sheets.repair_archive_headers(sheet_url, ARCHIVE_HEADER, tabs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """
    POST /api/scrape
    Body: { "profiles": [...], "maxPosts": 5 }
    """
    data = request.get_json(force=True)
    raw_profiles = data.get("profiles", [])
    max_posts = max(1, min(int(data.get("maxPosts", 5)), 50))
    token = APIFY_TOKEN or data.get("token", "")

    profile_urls = [u.strip() for u in raw_profiles if u.strip()]
    if not profile_urls:
        return jsonify({"error": "Please provide at least one LinkedIn URL."}), 400
    if not token:
        return jsonify({"error": "Apify API token is not configured on the server."}), 500

    def stream():
        try:
            client = ApifyClient(token)
            run_input = {
                "targetUrls": profile_urls,
                "maxPosts": max_posts,
                "maxReactions": 0,
                "postNestedReactions": False,
                "maxComments": 0,
                "postNestedComments": False,
            }
            yield "data: " + json.dumps({"status": "running"}) + "\n\n"

            run = client.actor(PROFILE_ACTOR).call(run_input=run_input)
            posts = list(client.dataset(get_dataset_id(run)).iterate_items())
            grouped = group_posts(posts, profile_urls)

            for url in profile_urls:
                yield "data: " + json.dumps({
                    "status": "profile",
                    "profileUrl": url,
                    "posts": grouped.get(url, []),
                }) + "\n\n"

            yield "data: " + json.dumps({"status": "done", "total": len(posts)}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"status": "error", "message": str(e)}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")

@app.route("/api/keyword-scrape", methods=["POST"])
def keyword_scrape():
    """
    POST /api/keyword-scrape
    Body: { "keywords": ["keyword1", ...], "perKeyword": 10, "date": "last-1-week" }
    Returns SSE with the posts found for every keyword.

    One actor run per keyword. The actor's `limit` is a global cap and it works
    through keywords in order, so a single call with many keywords only ever
    searches the first one.
    """
    data = request.get_json(force=True)
    keywords = clean_keywords(data.get("keywords", []))
    per_keyword = max(1, int(data.get("perKeyword", data.get("limit", 10)) or 10))
    date_filter = data.get("date", "last-1-week")
    token = APIFY_TOKEN or data.get("token", "")

    if not keywords:
        return jsonify({"error": "Please provide at least one keyword."}), 400
    if not token:
        return jsonify({"error": "Apify API token is not configured."}), 500
    if len(keywords) > 30:
        return jsonify({
            "error": f"{len(keywords)} keywords is too many for a live run "
                     f"(each is a separate actor call). Use the one-click sweep instead — "
                     f"it runs in the background and reports progress.",
        }), 400

    def stream():
        try:
            client = ApifyClient(token)
            yield "data: " + json.dumps({
                "status": "running",
                "step": f"Searching {len(keywords)} keywords…",
                "estimatedCost": scrapers.estimate_sweep_cost(len(keywords), per_keyword),
            }) + "\n\n"

            raw_rows, report = scrapers.sweep_keywords(
                client, keywords, date_filter, per_keyword
            )
            unique, extras = scrapers.dedupe_keyword_results(raw_rows)

            if not unique:
                yield "data: " + json.dumps({
                    "status": "results", "keywords": keywords, "posts": [], "total": 0,
                    "report": report,
                }) + "\n\n"
                yield "data: " + json.dumps({"status": "done", "total": 0}) + "\n\n"
                return

            urls = [r["post_url"] for r in unique if r.get("post_url")]
            yield "data: " + json.dumps({
                "status": "running",
                "step": f"Found {len(urls)} unique posts. Fetching content…",
            }) + "\n\n"

            content = scrapers.fetch_post_content(client, urls)
            url_to_kw = {scrapers.normalize_post_url(r.get("post_url", "")): r.get("keyword", "")
                         for r in unique}

            enriched = []
            for post in content:
                cleaned = clean_post(post)
                query = post.get("query") or {}
                target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                key = scrapers.normalize_post_url(target) or scrapers.normalize_post_url(
                    cleaned.get("postUrl", "")
                )
                cleaned["keyword"] = url_to_kw.get(key, "")
                others = [k for k in extras.get(key, []) if k and k != cleaned["keyword"]]
                cleaned["alsoMatchedKeywords"] = ", ".join(others)
                enriched.append(cleaned)

            enriched.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)

            yield "data: " + json.dumps({
                "status": "results",
                "keywords": keywords,
                "posts": enriched,
                "total": len(enriched),
                "report": report,
            }) + "\n\n"
            yield "data: " + json.dumps({"status": "done", "total": len(enriched)}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"status": "error", "message": str(e)}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/read-sheet", methods=["POST"])
def read_sheet():
    """
    POST /api/read-sheet
    Body: { "sheetUrl": "https://docs.google.com/spreadsheets/d/...", "sheetName": "Sheet1", "column": 1 }
    Returns list of values from the given column.
    """
    data = request.get_json(force=True)
    sheet_url = data.get("sheetUrl", "").strip()
    sheet_name = data.get("sheetName") or None
    col = int(data.get("column", 1))

    if not sheet_url:
        return jsonify({"error": "Sheet URL is required."}), 400

    try:
        values = read_sheet_column(sheet_url, sheet_name, col)
        return jsonify({"values": values, "count": len(values)})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to read sheet: {str(e)}"}), 500


@app.route("/api/export-sheet", methods=["POST"])
def export_sheet():
    """
    POST /api/export-sheet
    Body: { "sheetUrl": "...", "sheetName": "Export", "data": [...] }
    Writes data rows to the Google Sheet.
    """
    data = request.get_json(force=True)
    sheet_url = data.get("sheetUrl", "").strip()
    sheet_name = data.get("sheetName") or "Export"
    rows = data.get("data", [])

    if not sheet_url:
        return jsonify({"error": "Sheet URL is required."}), 400
    if not rows:
        return jsonify({"error": "No data to export."}), 400

    try:
        export_to_sheet(sheet_url, rows, sheet_name)
        return jsonify({"success": True, "rowsWritten": len(rows)})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Export failed: {str(e)}"}), 500


@app.route("/api/upload-excel", methods=["POST"])
def upload_excel():
    """
    POST /api/upload-excel
    Accepts an Excel file, optional sheet name, and column number.
    Returns values from that column (skipping header row).
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    col = int(request.form.get("column", 1))
    sheet_name = request.form.get("sheet_name", "").strip() or None

    if not file.filename:
        return jsonify({"error": "Empty filename."}), 400

    try:
        wb = load_workbook(filename=io.BytesIO(file.read()), read_only=True, data_only=True)

        # Select sheet by name or default to active
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                available = ", ".join(wb.sheetnames)
                wb.close()
                return jsonify({"error": f"Sheet '{sheet_name}' not found. Available: {available}"}), 400
            ws = wb[sheet_name]
        else:
            ws = wb.active

        values = []
        for row_idx, row in enumerate(ws.iter_rows(min_col=col, max_col=col, values_only=True), 1):
            if row_idx == 1:
                continue  # skip header
            val = row[0]
            if val and str(val).strip():
                values.append(str(val).strip())
        wb.close()
        return jsonify({"values": values, "count": len(values)})
    except Exception as e:
        return jsonify({"error": f"Failed to read Excel file: {str(e)}"}), 500


@app.route("/api/download-excel", methods=["POST"])
def download_excel():
    """
    POST /api/download-excel
    Body: { "data": [ {col: val, ...}, ... ], "filename": "results.xlsx" }
    Returns an Excel file download.
    """
    data = request.get_json(force=True)
    rows = data.get("data", [])
    filename = data.get("filename", "linkedin_posts.xlsx")

    if not rows:
        return jsonify({"error": "No data to export."}), 400

    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "Posts"

        # Write headers
        headers = list(rows[0].keys())
        ws.append(headers)

        # Write data
        for row in rows:
            ws.append([row.get(h, "") for h in headers])

        # Auto-width columns (approximate)
        for col_idx, header in enumerate(headers, 1):
            max_len = len(str(header))
            for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx, values_only=True):
                cell_len = len(str(row[0] or ""))
                if cell_len > max_len:
                    max_len = cell_len
            ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = min(max_len + 2, 60)

        # Save to buffer
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to create Excel: {str(e)}"}), 500


@app.route("/api/run-permanent", methods=["POST"])
def run_permanent():
    """One-click run over the permanent sheet.

    Delegates to the background sweep: 295 keywords is ~15 minutes of work, far
    past the 600s request timeout, so this returns a job id and the page polls
    /api/jobs/<id>. Results are written to the sheet as each stage finishes.
    """
    if not APIFY_TOKEN:
        return jsonify({"error": "Apify API token not configured."}), 500

    data = request.get_json(silent=True) or {}
    max_posts = int(data.get("maxPosts", 5))
    per_keyword = max(1, int(data.get("perKeyword", data.get("kwLimit", 10)) or 10))
    kw_date = data.get("kwDate", "last-1-week")

    try:
        profiles = read_sheet_column(PERMANENT_SHEET_URL, "Profiles")
        keywords = clean_keywords(read_sheet_column(PERMANENT_SHEET_URL, "Keywords"))
    except Exception as e:
        return jsonify({"error": f"Failed to read sheet: {str(e)}"}), 500

    if not profiles and not keywords:
        return jsonify({"error": "No profiles or keywords found in sheet."}), 400

    job = jobs.create("run-permanent")
    jobs.start(job, run_sweep, APIFY_TOKEN, profiles, keywords,
               max_posts, per_keyword, kw_date, PERMANENT_SHEET_URL)
    return jsonify({
        "jobId": job.id,
        "profiles": len(profiles),
        "keywords": len(keywords),
        "estimatedCost": scrapers.estimate_sweep_cost(len(keywords), per_keyword),
    })


@app.route("/api/run-all-json", methods=["POST"])
def run_all_json():
    """
    POST /api/run-all-json
    Body: { "profiles": [...], "keywords": [...], "maxPosts": 5, "kwLimit": "20", "kwDate": "last-1-week" }
    Same as run-all but takes pre-loaded data instead of a file.
    """
    data = request.get_json(force=True)
    profiles = [u.strip() for u in data.get("profiles", []) if u.strip()]
    keywords_raw = [k.strip() for k in data.get("keywords", []) if k.strip()]
    max_posts = int(data.get("maxPosts", 5))
    per_keyword = max(1, int(data.get("perKeyword", data.get("kwLimit", 10)) or 10))
    kw_date = data.get("kwDate", "last-1-week")
    token = APIFY_TOKEN

    if not token:
        return jsonify({"error": "Apify API token not configured."}), 500
    if not profiles and not keywords_raw:
        return jsonify({"error": "No profiles or keywords provided."}), 400

    keywords = clean_keywords(keywords_raw)

    def stream():
        client = ApifyClient(token)
        profile_posts_raw = []
        keyword_posts = []
        combo_posts = []

        if profiles:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Scraping {len(profiles)} profiles…"}) + "\n\n"
                run = client.actor(PROFILE_ACTOR).call(run_input={
                    "targetUrls": profiles, "maxPosts": max_posts,
                    "maxReactions": 0, "postNestedReactions": False, "maxComments": 0, "postNestedComments": False,
                })
                profile_posts_raw = list(client.dataset(get_dataset_id(run)).iterate_items())
                profile_cleaned = [clean_post(p) for p in profile_posts_raw]
                profile_cleaned.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
                yield "data: " + json.dumps({"status": "profileResults", "posts": profile_cleaned, "total": len(profile_cleaned)}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "profileResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        if keywords:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Searching {len(keywords)} keywords…"}) + "\n\n"
                # One actor run per keyword — a single call with the whole list
                # only ever searches the first keyword.
                raw_rows, kw_report = scrapers.sweep_keywords(
                    client, keywords, kw_date, per_keyword
                )
                unique, extras = scrapers.dedupe_keyword_results(raw_rows)
                post_urls = [r["post_url"] for r in unique if r.get("post_url")]
                if post_urls:
                    yield "data: " + json.dumps({"status": "running", "step": f"Fetching content for {len(post_urls)} unique keyword posts…"}) + "\n\n"
                    content_posts = scrapers.fetch_post_content(client, post_urls)
                    url_to_kw = {scrapers.normalize_post_url(r.get("post_url", "")): r.get("keyword", "")
                                 for r in unique}
                    for p in content_posts:
                        cleaned = clean_post(p)
                        query = p.get("query") or {}
                        target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                        key = scrapers.normalize_post_url(target) or scrapers.normalize_post_url(cleaned.get("postUrl", ""))
                        cleaned["keyword"] = url_to_kw.get(key, "")
                        others = [k for k in extras.get(key, []) if k and k != cleaned["keyword"]]
                        cleaned["alsoMatchedKeywords"] = ", ".join(others)
                        keyword_posts.append(cleaned)
                    keyword_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
                yield "data: " + json.dumps({"status": "keywordResults", "posts": keyword_posts, "total": len(keyword_posts), "report": kw_report}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        if profiles and keywords:
            yield "data: " + json.dumps({"status": "running", "step": "Filtering profile posts by keywords…"}) + "\n\n"
            patterns = compile_keyword_patterns(keywords)
            for p in profile_posts_raw:
                text = p.get("content") or p.get("text") or p.get("description") or ""
                matched = match_keywords(text, patterns)
                if matched:
                    cleaned = clean_post(p)
                    cleaned["keyword"] = ", ".join(matched)
                    combo_posts.append(cleaned)
            combo_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
            yield "data: " + json.dumps({"status": "comboResults", "posts": combo_posts, "total": len(combo_posts)}) + "\n\n"

        yield "data: " + json.dumps({"status": "done"}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/run-all", methods=["POST"])
def run_all():
    """
    POST /api/run-all (multipart form)
    Accepts: file, profilesTab, keywordsTab, maxPosts, kwLimit, kwDate
    Runs all 3 scrapers and returns SSE with results for each.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    profiles_tab = request.form.get("profilesTab", "profiles").strip()
    keywords_tab = request.form.get("keywordsTab", "keywords").strip()
    max_posts = int(request.form.get("maxPosts", 5))
    per_keyword = max(1, int(request.form.get("perKeyword", request.form.get("kwLimit", 10)) or 10))
    kw_date = request.form.get("kwDate", "last-1-week")
    token = APIFY_TOKEN

    if not token:
        return jsonify({"error": "Apify API token not configured."}), 500

    # Read both tabs from the Excel file
    try:
        wb = load_workbook(filename=io.BytesIO(file.read()), read_only=True, data_only=True)

        profiles = []
        if profiles_tab in wb.sheetnames:
            ws = wb[profiles_tab]
            for idx, row in enumerate(ws.iter_rows(min_col=1, max_col=1, values_only=True), 1):
                if idx == 1: continue
                if row[0] and str(row[0]).strip():
                    profiles.append(str(row[0]).strip())

        keywords_raw = []
        if keywords_tab in wb.sheetnames:
            ws = wb[keywords_tab]
            for idx, row in enumerate(ws.iter_rows(min_col=1, max_col=1, values_only=True), 1):
                if idx == 1: continue
                if row[0] and str(row[0]).strip():
                    keywords_raw.append(str(row[0]).strip())

        wb.close()
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {str(e)}"}), 500

    if not profiles and not keywords_raw:
        return jsonify({"error": f"No data found. Check tab names ('{profiles_tab}', '{keywords_tab}')."}), 400

    keywords = clean_keywords(keywords_raw)

    def stream():
        client = ApifyClient(token)
        profile_posts_raw = []
        keyword_posts = []
        combo_posts = []

        # ── 1. Profile Posts ────────────────────────────────────────────
        if profiles:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Scraping {len(profiles)} profiles…"}) + "\n\n"
                run = client.actor(PROFILE_ACTOR).call(run_input={
                    "targetUrls": profiles,
                    "maxPosts": max_posts,
                    "maxReactions": 0,
                    "postNestedReactions": False,
                    "maxComments": 0,
                    "postNestedComments": False,
                })
                profile_posts_raw = list(client.dataset(get_dataset_id(run)).iterate_items())
                profile_cleaned = [clean_post(p) for p in profile_posts_raw]
                profile_cleaned.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
                yield "data: " + json.dumps({
                    "status": "profileResults",
                    "posts": profile_cleaned,
                    "total": len(profile_cleaned),
                }) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "profileResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        # ── 2. Keyword Posts ────────────────────────────────────────────
        if keywords:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Searching {len(keywords)} keywords…"}) + "\n\n"
                # One actor run per keyword — a single call with the whole list
                # only ever searches the first keyword.
                raw_rows, kw_report = scrapers.sweep_keywords(
                    client, keywords, kw_date, per_keyword
                )
                unique, extras = scrapers.dedupe_keyword_results(raw_rows)
                post_urls = [r["post_url"] for r in unique if r.get("post_url")]
                if post_urls:
                    yield "data: " + json.dumps({"status": "running", "step": f"Fetching content for {len(post_urls)} unique keyword posts…"}) + "\n\n"
                    content_posts = scrapers.fetch_post_content(client, post_urls)
                    url_to_kw = {scrapers.normalize_post_url(r.get("post_url", "")): r.get("keyword", "")
                                 for r in unique}
                    for p in content_posts:
                        cleaned = clean_post(p)
                        query = p.get("query") or {}
                        target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                        key = scrapers.normalize_post_url(target) or scrapers.normalize_post_url(cleaned.get("postUrl", ""))
                        cleaned["keyword"] = url_to_kw.get(key, "")
                        others = [k for k in extras.get(key, []) if k and k != cleaned["keyword"]]
                        cleaned["alsoMatchedKeywords"] = ", ".join(others)
                        keyword_posts.append(cleaned)
                    keyword_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
                yield "data: " + json.dumps({
                    "status": "keywordResults",
                    "posts": keyword_posts,
                    "total": len(keyword_posts),
                    "report": kw_report,
                }) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        # ── 3. Combo: Profile posts filtered by keywords ────────────────
        if profiles and keywords:
            yield "data: " + json.dumps({"status": "running", "step": "Filtering profile posts by keywords…"}) + "\n\n"
            patterns = compile_keyword_patterns(keywords)
            for p in profile_posts_raw:
                text = p.get("content") or p.get("text") or p.get("description") or ""
                if not text:
                    continue
                matched = match_keywords(text, patterns)
                if matched:
                    cleaned = clean_post(p)
                    cleaned["keyword"] = ", ".join(matched[:3])
                    combo_posts.append(cleaned)
            combo_posts.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)
            yield "data: " + json.dumps({
                "status": "comboResults",
                "posts": combo_posts,
                "total": len(combo_posts),
            }) + "\n\n"

        yield "data: " + json.dumps({"status": "done"}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/download-results", methods=["POST"])
def download_results():
    """
    POST /api/download-results
    Body: { "profiles": [...], "keywords": [...], "combo": [...] }
    Returns Excel with 3 tabs.
    """
    data = request.get_json(force=True)
    profile_rows = data.get("profiles", [])
    keyword_rows = data.get("keywords", [])
    combo_rows = data.get("combo", [])

    wb = Workbook()

    # Tab 1: Profile Posts
    ws1 = wb.active
    ws1.title = "Profile Posts"
    if profile_rows:
        headers = list(profile_rows[0].keys())
        ws1.append(headers)
        for row in profile_rows:
            ws1.append([str(row.get(h, ""))[:32000] for h in headers])
    else:
        ws1.append(["No data"])

    # Tab 2: Keyword Posts
    ws2 = wb.create_sheet("Keyword Posts")
    if keyword_rows:
        headers = list(keyword_rows[0].keys())
        ws2.append(headers)
        for row in keyword_rows:
            ws2.append([str(row.get(h, ""))[:32000] for h in headers])
    else:
        ws2.append(["No data"])

    # Tab 3: Profile + Keywords
    ws3 = wb.create_sheet("Profile + Keywords")
    if combo_rows:
        headers = list(combo_rows[0].keys())
        ws3.append(headers)
        for row in combo_rows:
            ws3.append([str(row.get(h, ""))[:32000] for h in headers])
    else:
        ws3.append(["No data"])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="linkedin_results.xlsx",
    )


if __name__ == "__main__":
    # Local development only. In production (Render) the app is served by
    # gunicorn — see Procfile / render.yaml.
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
