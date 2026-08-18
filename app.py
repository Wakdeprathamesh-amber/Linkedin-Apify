"""
Flask web app for LinkedIn Post Scraper
- Profile posts via harvestapi/linkedin-profile-posts
- Keyword posts via sasky/linkedin-keyword-posts-urls-scraper
- Google Sheets as input source and output destination
"""

import os
import re
import json
import io
from functools import wraps
from datetime import datetime
from flask import (
    Flask, render_template, request, jsonify, Response, send_file,
    session, redirect, url_for,
)
from apify_client import ApifyClient
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook

load_dotenv(override=True)

app = Flask(__name__)
# Session secret — MUST be set via env in production (Render env var SECRET_KEY)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")

PROFILE_ACTOR = "harvestapi/linkedin-profile-posts"
KEYWORD_ACTOR = "sasky/linkedin-keyword-posts-urls-scraper"
PERMANENT_SHEET_URL = os.environ.get(
    "PERMANENT_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/18IbGRZ-aJWI2QVxjHS1zZBo8o-IKrndsi4zu-w7jT4Q",
)
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")

# ─── Login credentials (static for now, overridable via env) ─────────────────
APP_USERNAME = os.environ.get("APP_USERNAME", "Amber")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Amber@123")

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


# ─── Google Sheets helpers ──────────────────────────────────────────────────

def get_gspread_client():
    """Return authenticated gspread client using a service account.

    Resolution order:
      1. GOOGLE_CREDENTIALS_JSON env var (raw JSON) — used on Render/cloud.
      2. credentials.json file on disk — used for local development.
    """
    import gspread
    if GOOGLE_CREDENTIALS_JSON.strip():
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
        except json.JSONDecodeError as e:
            raise ValueError(
                "GOOGLE_CREDENTIALS_JSON is set but is not valid JSON: " + str(e)
            )
        return gspread.service_account_from_dict(info)
    if os.path.exists(GSHEET_CREDS):
        return gspread.service_account(filename=GSHEET_CREDS)
    raise FileNotFoundError(
        "Google credentials not found. Set the GOOGLE_CREDENTIALS_JSON env var "
        f"(full service-account JSON) or provide a file at '{GSHEET_CREDS}'."
    )


def read_sheet_column(sheet_url: str, sheet_name: str = None, col: int = 1) -> list:
    """Read all non-empty values from a specific column in a Google Sheet."""
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)
    ws = spreadsheet.worksheet(sheet_name) if sheet_name else spreadsheet.sheet1
    values = ws.col_values(col)
    # Skip header row and filter empty
    return [v.strip() for v in values[1:] if v.strip()]


def export_to_sheet(sheet_url: str, rows: list, sheet_name: str = None):
    """
    Write rows to a Google Sheet tab with a single rolling archive.

    For each output type there are only ever TWO tabs:
      - "<name>"            → the latest run (overwritten every time)
      - "<name> - Archive"  → all previous runs, appended (accumulates)

    On each run, whatever is currently in "<name>" is appended to
    "<name> - Archive" before "<name>" is overwritten with the new data.
    Every row carries a scrapeDate column so runs stay distinguishable
    inside the central archive.
    """
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)

    target_name = sheet_name or "Export"
    archive_name = f"{target_name} - Archive"
    today = datetime.now().strftime("%Y-%m-%d")

    # ── Build the fresh data block (header + rows, each tagged scrapeDate) ──
    if rows:
        headers = list(rows[0].keys())
        if "scrapeDate" not in headers:
            headers.append("scrapeDate")
        new_data = [headers]
        for row in rows:
            row_data = [str(row.get(h, "")) for h in headers[:-1]]
            row_data.append(today)
            new_data.append(row_data)
    else:
        new_data = [["No data"]]

    # ── 1. Move the current tab's contents into the central archive ────────
    try:
        current_ws = spreadsheet.worksheet(target_name)
    except Exception:
        current_ws = None

    if current_ws is not None:
        existing = current_ws.get_all_values()
        # Only archive real data (header + ≥1 row, not the "No data" placeholder)
        if len(existing) > 1 and existing[0] != ["No data"]:
            header = existing[0]
            data_rows = existing[1:]
            try:
                archive_ws = spreadsheet.worksheet(archive_name)
                archive_has_data = len(archive_ws.get_all_values()) > 0
            except Exception:
                archive_ws = spreadsheet.add_worksheet(
                    title=archive_name,
                    rows=max(len(data_rows) + 10, 100),
                    cols=max(len(header), 20),
                )
                archive_has_data = False
            if archive_has_data:
                archive_ws.append_rows(data_rows, value_input_option="RAW")
            else:
                archive_ws.append_rows([header] + data_rows, value_input_option="RAW")

    # ── 2. Overwrite the current tab with the fresh run ────────────────────
    if current_ws is not None:
        current_ws.clear()
        ws = current_ws
    else:
        ws = spreadsheet.add_worksheet(
            title=target_name, rows=max(len(new_data) + 10, 100), cols=20
        )

    ws.update(values=new_data, range_name="A1")


# ─── Apify helpers ──────────────────────────────────────────────────────────

def extract_timestamp(post: dict) -> int:
    raw = post.get("postedAt") or post.get("createdAt") or post.get("publishedAt") or 0
    if isinstance(raw, dict):
        return int(raw.get("timestamp") or 0)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0
    return 0


def get_source_url(post: dict) -> str:
    query = post.get("query") or {}
    if isinstance(query, dict) and query.get("targetUrl"):
        return query["targetUrl"].rstrip("/").lower()
    author = post.get("author") or {}
    return (
        author.get("url") or author.get("profileUrl")
        or post.get("authorUrl") or post.get("profileUrl") or ""
    ).rstrip("/").lower()


def clean_post(post: dict) -> dict:
    author = post.get("author") or {}

    posted_at_raw = post.get("postedAt") or post.get("createdAt") or post.get("publishedAt") or {}
    timestamp_ms = extract_timestamp(post)

    if isinstance(posted_at_raw, dict):
        posted_at_str = posted_at_raw.get("date") or posted_at_raw.get("postedAgoText") or ""
    elif isinstance(posted_at_raw, str):
        posted_at_str = posted_at_raw
    else:
        posted_at_str = ""

    return {
        "authorName": author.get("name") or post.get("authorName") or "Unknown",
        "authorUrl": (
            author.get("url") or author.get("profileUrl")
            or post.get("authorUrl") or post.get("profileUrl") or ""
        ),
        "authorImage": (
            author.get("image") or author.get("profilePicture")
            or author.get("avatar") or post.get("authorImage") or ""
        ),
        "postUrl": (
            post.get("linkedinUrl")
            or post.get("shareLinkedinUrl")
            or post.get("url")
            or post.get("postUrl")
            or post.get("link")
            or ""
        ),
        "postedAt": posted_at_str,
        "timestampMs": timestamp_ms,
        "text": (
            post.get("content")
            or post.get("text")
            or post.get("description")
            or ""
        ),
        "reactionsCount": int(
            (post.get("engagement") or {}).get("likes")
            or post.get("reactionsCount")
            or post.get("likesCount")
            or post.get("reactions")
            or 0
        ),
        "commentsCount": int(
            (post.get("engagement") or {}).get("comments")
            or post.get("commentsCount")
            or post.get("comments")
            or 0
        ),
        "sharesCount": int(
            (post.get("engagement") or {}).get("shares")
            or post.get("sharesCount")
            or post.get("shares")
            or 0
        ),
        "images": post.get("images") or post.get("media") or [],
    }


def group_posts(posts: list, profile_urls: list) -> dict:
    grouped = {url: [] for url in profile_urls}
    for post in posts:
        source = get_source_url(post)
        cleaned = clean_post(post)
        matched = False
        for url in profile_urls:
            norm = url.rstrip("/").lower()
            if source and (source == norm or source.startswith(norm) or norm.startswith(source)):
                grouped[url].append(cleaned)
                matched = True
                break
        if not matched and profile_urls:
            grouped[profile_urls[0]].append(cleaned)
    for url in profile_urls:
        grouped[url].sort(key=lambda p: p["timestampMs"], reverse=True)
    return grouped


def get_dataset_id(run) -> str:
    return run.default_dataset_id if hasattr(run, "default_dataset_id") else run["defaultDatasetId"]


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
    """Unauthenticated health check for Render."""
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template("index.html", user=session.get("user", ""))


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
    Body: { "keywords": ["keyword1", ...], "limit": "20", "date": "last-1-week" }
    Returns SSE with post URLs grouped by keyword.
    """
    data = request.get_json(force=True)
    keywords = [k.strip() for k in data.get("keywords", []) if k.strip()]
    limit = str(data.get("limit", "20"))
    date_filter = data.get("date", "ignore")
    token = APIFY_TOKEN or data.get("token", "")

    if not keywords:
        return jsonify({"error": "Please provide at least one keyword."}), 400
    if not token:
        return jsonify({"error": "Apify API token is not configured."}), 500

    # Clean keywords — strip bullet prefixes like "- ", "• ", "* "
    import re
    keywords = [re.sub(r'^[\-\•\*]\s*', '', k).strip() for k in keywords]
    # Remove special characters that LinkedIn search doesn't support
    keywords = [re.sub(r'[&\+\#\@\!\?\*]', ' ', k).strip() for k in keywords]
    # Collapse multiple spaces
    keywords = [re.sub(r'\s+', ' ', k) for k in keywords]
    # Remove section headers (entries ending with ":" like "Australia:", "Other Regions:")
    keywords = [k for k in keywords if not k.endswith(':')]
    # Remove entries that are too short (less than 3 chars) or empty
    keywords = [k for k in keywords if len(k) >= 3]

    def stream():
        try:
            client = ApifyClient(token)
            run_input = {
                "keywords": keywords,
                "limit": limit,
                "date": date_filter,
            }
            yield "data: " + json.dumps({"status": "running", "step": "Finding post URLs..."}) + "\n\n"

            run = client.actor(KEYWORD_ACTOR).call(run_input=run_input)

            # Check if run succeeded
            run_status = run.status if hasattr(run, "status") else run.get("status", "")
            if run_status == "FAILED":
                msg = run.status_message if hasattr(run, "status_message") else "Actor run failed."
                yield "data: " + json.dumps({"status": "error", "message": str(msg)}) + "\n\n"
                return

            results = list(client.dataset(get_dataset_id(run)).iterate_items())

            if not results:
                yield "data: " + json.dumps({"status": "results", "keywords": keywords, "posts": [], "total": 0}) + "\n\n"
                yield "data: " + json.dumps({"status": "done", "total": 0}) + "\n\n"
                return

            # Step 2: Fetch actual post content using the profile posts scraper
            post_urls = [r.get("post_url") for r in results if r.get("post_url")]
            if not post_urls:
                yield "data: " + json.dumps({"status": "results", "keywords": keywords, "posts": results, "total": len(results)}) + "\n\n"
                yield "data: " + json.dumps({"status": "done", "total": len(results)}) + "\n\n"
                return

            yield "data: " + json.dumps({"status": "running", "step": f"Found {len(post_urls)} posts. Fetching content..."}) + "\n\n"

            # Use the profile posts scraper to get full content from URLs
            content_run = client.actor(PROFILE_ACTOR).call(run_input={
                "targetUrls": post_urls,
                "maxPosts": 1,
                "maxReactions": 0,
                "postNestedReactions": False,
                "maxComments": 0,
                "postNestedComments": False,
            })

            content_posts = list(client.dataset(get_dataset_id(content_run)).iterate_items())

            # Clean and enrich with keyword info
            enriched = []
            # Build a lookup from URL results to keyword
            url_to_keyword = {r.get("post_url", ""): r.get("keyword", "") for r in results}

            for post in content_posts:
                cleaned = clean_post(post)
                # Try to match back to keyword
                post_url = post.get("url") or post.get("postUrl") or post.get("link") or ""
                query = post.get("query") or {}
                target_url = query.get("targetUrl", "") if isinstance(query, dict) else ""
                matched_keyword = url_to_keyword.get(target_url, "")
                cleaned["keyword"] = matched_keyword
                enriched.append(cleaned)

            # Sort by timestamp
            enriched.sort(key=lambda p: p.get("timestampMs", 0), reverse=True)

            yield "data: " + json.dumps({
                "status": "results",
                "keywords": keywords,
                "posts": enriched,
                "total": len(enriched),
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
    """
    One-click run: reads from permanent sheet (Profiles + Keywords tabs),
    runs all scrapers, appends results to 3 output tabs in the same sheet.
    """
    token = APIFY_TOKEN
    if not token:
        return jsonify({"error": "Apify API token not configured."}), 500

    # Read input from permanent sheet
    try:
        gc = get_gspread_client()
        spreadsheet = gc.open_by_url(PERMANENT_SHEET_URL)

        profiles = []
        try:
            ws = spreadsheet.worksheet("Profiles")
            vals = ws.col_values(1)
            profiles = [v.strip() for v in vals[1:] if v.strip()]
        except Exception:
            pass

        keywords_raw = []
        try:
            ws = spreadsheet.worksheet("Keywords")
            vals = ws.col_values(1)
            keywords_raw = [v.strip() for v in vals[1:] if v.strip()]
        except Exception:
            pass
    except Exception as e:
        return jsonify({"error": f"Failed to read sheet: {str(e)}"}), 500

    if not profiles and not keywords_raw:
        return jsonify({"error": "No profiles or keywords found in sheet."}), 400

    # Clean keywords
    import re
    keywords = [re.sub(r'^[\-\•\*]\s*', '', k).strip() for k in keywords_raw]
    keywords = [re.sub(r'[&\+\#\@\!\?\*]', ' ', k).strip() for k in keywords]
    keywords = [re.sub(r'\s+', ' ', k) for k in keywords]
    keywords = [k for k in keywords if not k.endswith(':')]
    keywords = [k for k in keywords if len(k) >= 3]

    max_posts = int(request.json.get("maxPosts", 5)) if request.is_json else 5
    kw_limit = request.json.get("kwLimit", "20") if request.is_json else "20"
    kw_date = request.json.get("kwDate", "last-1-week") if request.is_json else "last-1-week"

    def stream():
        client = ApifyClient(token)
        profile_posts_raw = []
        profile_cleaned = []
        keyword_posts = []
        combo_posts = []

        # ── 1. Profile Posts ──
        if profiles:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Scraping {len(profiles)} profiles…"}) + "\n\n"
                run = client.actor(PROFILE_ACTOR).call(run_input={
                    "targetUrls": profiles, "maxPosts": max_posts,
                    "maxReactions": 0, "postNestedReactions": False, "maxComments": 0, "postNestedComments": False,
                })
                profile_posts_raw = list(client.dataset(get_dataset_id(run)).iterate_items())
                profile_cleaned = [clean_post(p) for p in profile_posts_raw]
                profile_cleaned.sort(key=lambda p: p["timestampMs"], reverse=True)
                yield "data: " + json.dumps({"status": "profileResults", "posts": profile_cleaned, "total": len(profile_cleaned)}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "profileResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        # ── 2. Keyword Posts ──
        if keywords:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Searching {len(keywords)} keywords…"}) + "\n\n"
                run = client.actor(KEYWORD_ACTOR).call(run_input={"keywords": keywords, "limit": kw_limit, "date": kw_date})
                run_status = run.status if hasattr(run, "status") else ""
                if run_status == "FAILED":
                    yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": "Keyword actor failed"}) + "\n\n"
                else:
                    kw_results = list(client.dataset(get_dataset_id(run)).iterate_items())
                    post_urls = [r.get("post_url") for r in kw_results if r.get("post_url")]
                    if post_urls:
                        yield "data: " + json.dumps({"status": "running", "step": f"Fetching content for {len(post_urls)} keyword posts…"}) + "\n\n"
                        content_run = client.actor(PROFILE_ACTOR).call(run_input={
                            "targetUrls": post_urls, "maxPosts": 1,
                            "maxReactions": 0, "postNestedReactions": False, "maxComments": 0, "postNestedComments": False,
                        })
                        content_posts = list(client.dataset(get_dataset_id(content_run)).iterate_items())
                        url_to_kw = {r.get("post_url", ""): r.get("keyword", "") for r in kw_results}
                        for p in content_posts:
                            cleaned = clean_post(p)
                            query = p.get("query") or {}
                            target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                            cleaned["keyword"] = url_to_kw.get(target, "")
                            keyword_posts.append(cleaned)
                        keyword_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
                    yield "data: " + json.dumps({"status": "keywordResults", "posts": keyword_posts, "total": len(keyword_posts)}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        # ── 3. Combo ──
        if profiles and keywords:
            yield "data: " + json.dumps({"status": "running", "step": "Filtering profile posts by keywords…"}) + "\n\n"
            keyword_lower = [k.lower() for k in keywords]
            for p in profile_posts_raw:
                text = (p.get("content") or p.get("text") or p.get("description") or "").lower()
                matched = [k for k in keyword_lower if k in text]
                if matched:
                    cleaned = clean_post(p)
                    cleaned["keyword"] = ", ".join(matched)
                    combo_posts.append(cleaned)
            combo_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
            yield "data: " + json.dumps({"status": "comboResults", "posts": combo_posts, "total": len(combo_posts)}) + "\n\n"

        # ── 4. Export to permanent sheet (only Keywords and Combo, fresh tabs) ──
        yield "data: " + json.dumps({"status": "running", "step": "Saving results to Google Sheet…"}) + "\n\n"
        try:
            def make_rows(posts):
                return [{"authorName": p.get("authorName",""), "postedAt": p.get("postedAt",""),
                         "text": (p.get("text",""))[:5000], "postUrl": p.get("postUrl",""),
                         "reactions": p.get("reactionsCount",0), "comments": p.get("commentsCount",0),
                         "shares": p.get("sharesCount",0), "keyword": p.get("keyword","")} for p in posts]

            new_kw = 0
            new_combo = 0

            if keyword_posts:
                export_to_sheet(PERMANENT_SHEET_URL, make_rows(keyword_posts), "Keyword Posts")
                new_kw = len(keyword_posts)

            if combo_posts:
                export_to_sheet(PERMANENT_SHEET_URL, make_rows(combo_posts), "Profile + Keywords")
                new_combo = len(combo_posts)

            yield "data: " + json.dumps({"status": "running", "step": f"✅ Saved {new_kw} keyword posts, {new_combo} combo posts to sheet."}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"status": "running", "step": f"⚠️ Sheet export failed: {str(e)}"}) + "\n\n"

        yield "data: " + json.dumps({"status": "done"}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream")


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
    kw_limit = str(data.get("kwLimit", "20"))
    kw_date = data.get("kwDate", "last-1-week")
    token = APIFY_TOKEN

    if not token:
        return jsonify({"error": "Apify API token not configured."}), 500
    if not profiles and not keywords_raw:
        return jsonify({"error": "No profiles or keywords provided."}), 400

    # Clean keywords
    import re
    keywords = [re.sub(r'^[\-\•\*]\s*', '', k).strip() for k in keywords_raw]
    keywords = [re.sub(r'[&\+\#\@\!\?\*]', ' ', k).strip() for k in keywords]
    keywords = [re.sub(r'\s+', ' ', k) for k in keywords]
    keywords = [k for k in keywords if not k.endswith(':')]
    keywords = [k for k in keywords if len(k) >= 3]

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
                profile_cleaned.sort(key=lambda p: p["timestampMs"], reverse=True)
                yield "data: " + json.dumps({"status": "profileResults", "posts": profile_cleaned, "total": len(profile_cleaned)}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "profileResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        if keywords:
            try:
                yield "data: " + json.dumps({"status": "running", "step": f"Searching {len(keywords)} keywords…"}) + "\n\n"
                run = client.actor(KEYWORD_ACTOR).call(run_input={"keywords": keywords, "limit": kw_limit, "date": kw_date})
                run_status = run.status if hasattr(run, "status") else ""
                if run_status == "FAILED":
                    yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": "Keyword actor failed"}) + "\n\n"
                else:
                    kw_results = list(client.dataset(get_dataset_id(run)).iterate_items())
                    post_urls = [r.get("post_url") for r in kw_results if r.get("post_url")]
                    if post_urls:
                        yield "data: " + json.dumps({"status": "running", "step": f"Fetching content for {len(post_urls)} keyword posts…"}) + "\n\n"
                        content_run = client.actor(PROFILE_ACTOR).call(run_input={
                            "targetUrls": post_urls, "maxPosts": 1,
                            "maxReactions": 0, "postNestedReactions": False, "maxComments": 0, "postNestedComments": False,
                        })
                        content_posts = list(client.dataset(get_dataset_id(content_run)).iterate_items())
                        url_to_kw = {r.get("post_url", ""): r.get("keyword", "") for r in kw_results}
                        for p in content_posts:
                            cleaned = clean_post(p)
                            query = p.get("query") or {}
                            target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                            cleaned["keyword"] = url_to_kw.get(target, "")
                            keyword_posts.append(cleaned)
                        keyword_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
                    yield "data: " + json.dumps({"status": "keywordResults", "posts": keyword_posts, "total": len(keyword_posts)}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        if profiles and keywords:
            yield "data: " + json.dumps({"status": "running", "step": "Filtering profile posts by keywords…"}) + "\n\n"
            keyword_lower = [k.lower() for k in keywords]
            for p in profile_posts_raw:
                text = (p.get("content") or p.get("text") or p.get("description") or "").lower()
                matched = [k for k in keyword_lower if k in text]
                if matched:
                    cleaned = clean_post(p)
                    cleaned["keyword"] = ", ".join(matched)
                    combo_posts.append(cleaned)
            combo_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
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
    kw_limit = request.form.get("kwLimit", "20")
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

    # Clean keywords
    import re
    keywords = [re.sub(r'^[\-\•\*]\s*', '', k).strip() for k in keywords_raw]
    keywords = [re.sub(r'[&\+\#\@\!\?\*]', ' ', k).strip() for k in keywords]
    keywords = [re.sub(r'\s+', ' ', k) for k in keywords]
    keywords = [k for k in keywords if not k.endswith(':')]
    keywords = [k for k in keywords if len(k) >= 3]

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
                profile_cleaned.sort(key=lambda p: p["timestampMs"], reverse=True)
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
                run = client.actor(KEYWORD_ACTOR).call(run_input={
                    "keywords": keywords,
                    "limit": kw_limit,
                    "date": kw_date,
                })
                run_status = run.status if hasattr(run, "status") else ""
                if run_status == "FAILED":
                    yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": "Keyword actor failed"}) + "\n\n"
                else:
                    kw_results = list(client.dataset(get_dataset_id(run)).iterate_items())
                    # Fetch content for keyword post URLs
                    post_urls = [r.get("post_url") for r in kw_results if r.get("post_url")]
                    if post_urls:
                        yield "data: " + json.dumps({"status": "running", "step": f"Fetching content for {len(post_urls)} keyword posts…"}) + "\n\n"
                        content_run = client.actor(PROFILE_ACTOR).call(run_input={
                            "targetUrls": post_urls,
                            "maxPosts": 1,
                            "maxReactions": 0,
                            "postNestedReactions": False,
                            "maxComments": 0,
                            "postNestedComments": False,
                        })
                        content_posts = list(client.dataset(get_dataset_id(content_run)).iterate_items())
                        url_to_kw = {r.get("post_url", ""): r.get("keyword", "") for r in kw_results}
                        for p in content_posts:
                            cleaned = clean_post(p)
                            query = p.get("query") or {}
                            target = query.get("targetUrl", "") if isinstance(query, dict) else ""
                            cleaned["keyword"] = url_to_kw.get(target, "")
                            keyword_posts.append(cleaned)
                        keyword_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
                    yield "data: " + json.dumps({
                        "status": "keywordResults",
                        "posts": keyword_posts,
                        "total": len(keyword_posts),
                    }) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"status": "keywordResults", "posts": [], "total": 0, "error": str(e)}) + "\n\n"

        # ── 3. Combo: Profile posts filtered by keywords ────────────────
        if profiles and keywords:
            yield "data: " + json.dumps({"status": "running", "step": "Filtering profile posts by keywords…"}) + "\n\n"
            keyword_lower = [k.lower() for k in keywords]
            for p in profile_posts_raw:
                text = (p.get("content") or p.get("text") or p.get("description") or "").lower()
                if not text:
                    continue
                matched = []
                for kw in keyword_lower:
                    if kw in text:
                        matched.append(kw)
                    else:
                        words = [w for w in kw.split() if len(w) > 3]
                        if words and all(w in text for w in words):
                            matched.append(kw)
                if matched:
                    cleaned = clean_post(p)
                    cleaned["keyword"] = ", ".join(matched[:3])
                    combo_posts.append(cleaned)
            combo_posts.sort(key=lambda p: p["timestampMs"], reverse=True)
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
