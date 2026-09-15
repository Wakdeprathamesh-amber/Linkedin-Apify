"""Apify interaction: keyword fan-out, post fetching, cleaning, cost control.

The keyword actor's `limit` is a GLOBAL result cap for the whole run, not a
per-keyword one, and it works through the keyword list sequentially. Sending all
295 keywords in one call therefore spends the entire budget on keyword #1 — which
is why only one keyword was ever searched. We now run one actor call per keyword.
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal

PROFILE_ACTOR = "harvestapi/linkedin-profile-posts"
KEYWORD_ACTOR = "sasky/linkedin-keyword-posts-urls-scraper"

# Measured against live runs (Aug-18: 20 results billed $0.044).
KEYWORD_RUN_COST_USD = 0.044          # one keyword search, charged per run
POST_COST_USD = 0.0015                # one post fetched in stage 2
DEDUPE_FACTOR = 0.85                  # observed unique-post ratio across keywords

# Apify rejects any per-run cap below $0.70.
MAX_CHARGE_PER_RUN_USD = Decimal("0.70")
# The keyword actor refuses anything under 300s ("Low timeout! 300 sec is the
# minimum."), so this is the floor, not a preference. Observed runs finish in
# 75-110s, leaving ~3x headroom before a hung keyword is killed.
KEYWORD_RUN_TIMEOUT_S = 300
CONTENT_CHUNK_SIZE = 200
# Account cap is 32 concurrent jobs, shared with another project on this token.
# Measured: one keyword run takes 75-135s, so throughput is roughly
# CONCURRENCY keywords per ~130s -> 295 keywords at 12 concurrent is ~55 minutes.
DEFAULT_CONCURRENCY = 12

SUCCESS_STATUS = "SUCCEEDED"


class ActorError(RuntimeError):
    """An actor run did not finish successfully."""


# ─── Status ─────────────────────────────────────────────────────────────────

def is_successful(status) -> bool:
    """Only SUCCEEDED counts. FAILED, TIMED-OUT, ABORTED and ABORTING do not.

    The old code compared against "FAILED" alone, so an aborted or timed-out run
    was treated as a successful empty run.
    """
    return status == SUCCESS_STATUS


def get_dataset_id(run) -> str:
    return run.default_dataset_id if hasattr(run, "default_dataset_id") else run["defaultDatasetId"]


def _status_of(run):
    return run.status if hasattr(run, "status") else (run or {}).get("status", "")


# ─── Keywords ───────────────────────────────────────────────────────────────

def clean_keywords(raw: list) -> list:
    """Strip bullets and characters LinkedIn search rejects; drop section headers."""
    out = []
    for k in raw:
        k = re.sub(r"^[\-\•\*]\s*", "", (k or "").strip())
        k = re.sub(r"[&\+\#\@\!\?\*]", " ", k).strip()
        k = re.sub(r"\s+", " ", k)
        if not k or k.endswith(":") or len(k) < 3:
            continue
        out.append(k)
    return out


def compile_keyword_patterns(keywords: list) -> list:
    """Whole-word patterns, so "cas" stops matching "forecast" and "Newcastle".

    Uses lookarounds rather than \\b so keywords ending in punctuation still work.
    """
    return [
        (kw, re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE))
        for kw in keywords
    ]


def match_keywords(text: str, patterns: list) -> list:
    """Every keyword that appears as a whole word in `text`, in keyword order."""
    if not text:
        return []
    return [kw for kw, pattern in patterns if pattern.search(text)]


def take_per_keyword(rows: list, per_keyword: int) -> list:
    """Trim to the depth we asked for.

    The actor floors `limit` at 20 and returns a whole page regardless, so we pay
    for ~20 either way and trim client-side. Raising per_keyword from 10 to 20
    therefore costs nothing extra in stage 1 — only stage 2 grows.
    """
    return rows[:per_keyword] if per_keyword and per_keyword > 0 else rows


# ─── URLs and dedupe ────────────────────────────────────────────────────────

def normalize_post_url(url) -> str:
    """Strip query string and trailing slash so the same post compares equal."""
    if not url:
        return ""
    cleaned = str(url).split("?")[0].split("#")[0].rstrip("/")
    if "://" in cleaned:
        scheme, rest = cleaned.split("://", 1)
        host, _, path = rest.partition("/")
        cleaned = f"{scheme.lower()}://{host.lower()}" + (f"/{path}" if path else "")
    return cleaned


def dedupe_keyword_results(rows: list):
    """One row per unique post URL.

    Returns (unique_rows, url -> [every keyword that found it]). Deduping before
    stage 2 means we never pay to fetch the same post twice.
    """
    unique, extras = [], {}
    for row in rows:
        url = normalize_post_url(row.get("post_url", ""))
        if not url:
            continue
        if url in extras:
            kw = row.get("keyword", "")
            if kw and kw not in extras[url]:
                extras[url].append(kw)
            continue
        extras[url] = [row.get("keyword", "")] if row.get("keyword") else []
        unique.append(row)
    return unique, extras


# ─── Cost ───────────────────────────────────────────────────────────────────

def estimate_sweep_cost(n_keywords: int, per_keyword: int) -> float:
    """Stage 1 is charged per run, so depth only moves the stage-2 term."""
    stage1 = n_keywords * KEYWORD_RUN_COST_USD
    stage2 = n_keywords * per_keyword * DEDUPE_FACTOR * POST_COST_USD
    return round(stage1 + stage2, 2)


def evaluate_budget(estimated: float, used: float, cap: float, margin: float = 0.9) -> dict:
    """Refuse a run that would eat more than `margin` of what is left this month."""
    remaining = round(cap - used, 2)
    return {
        "ok": estimated <= remaining * margin,
        "estimated": round(estimated, 2),
        "used": round(used, 2),
        "cap": round(cap, 2),
        "remaining": remaining,
    }


def fetch_budget(token: str) -> dict:
    """Current monthly spend and cap from Apify."""
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://api.apify.com/v2/users/me/limits",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)["data"]
    return {
        "used": float(data.get("current", {}).get("monthlyUsageUsd", 0) or 0),
        "cap": float(data.get("limits", {}).get("maxMonthlyUsageUsd", 0) or 0),
    }


# ─── Post cleaning ──────────────────────────────────────────────────────────

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

    image = author.get("image") or author.get("profilePicture") or author.get("avatar") or ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return {
        "authorName": author.get("name") or post.get("authorName") or "Unknown",
        "authorUrl": (
            author.get("url") or author.get("profileUrl") or author.get("linkedinUrl")
            or post.get("authorUrl") or post.get("profileUrl") or ""
        ),
        "authorImage": image or post.get("authorImage") or "",
        "postUrl": (
            post.get("linkedinUrl") or post.get("shareLinkedinUrl") or post.get("url")
            or post.get("postUrl") or post.get("link") or ""
        ),
        "postedAt": posted_at_str,
        "timestampMs": timestamp_ms,
        "text": post.get("content") or post.get("text") or post.get("description") or "",
        "reactionsCount": int(
            (post.get("engagement") or {}).get("likes")
            or post.get("reactionsCount") or post.get("likesCount") or post.get("reactions") or 0
        ),
        "commentsCount": int(
            (post.get("engagement") or {}).get("comments")
            or post.get("commentsCount") or post.get("comments") or 0
        ),
        "sharesCount": int(
            (post.get("engagement") or {}).get("shares")
            or post.get("sharesCount") or post.get("shares") or 0
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


# ─── Actor calls ────────────────────────────────────────────────────────────

def search_one_keyword(client, keyword: str, date_filter: str, per_keyword: int) -> list:
    """One actor run for exactly one keyword. Raises ActorError if it fails."""
    run = client.actor(KEYWORD_ACTOR).call(
        run_input={"keywords": [keyword], "limit": str(per_keyword), "date": date_filter},
        run_timeout=timedelta(seconds=KEYWORD_RUN_TIMEOUT_S),
        max_total_charge_usd=MAX_CHARGE_PER_RUN_USD,
        logger=None,  # 295 concurrent actor log streams would drown the app log
    )
    if run is None:
        raise ActorError("actor did not start")

    status = _status_of(run)
    if not is_successful(status):
        raise ActorError(f"run finished as {status or 'UNKNOWN'}")

    return list(client.dataset(get_dataset_id(run)).iterate_items())


def sweep_keywords(client, keywords: list, date_filter: str, per_keyword: int,
                   concurrency: int = DEFAULT_CONCURRENCY,
                   on_progress=None, should_cancel=None):
    """Search every keyword, one actor run each, fanned out concurrently.

    Returns (rows, report). A keyword that fails is recorded in the report and
    the sweep continues.
    """
    rows, report = [], []

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(search_one_keyword, client, kw, date_filter, per_keyword): kw
            for kw in keywords
        }
        for future in as_completed(futures):
            keyword = futures[future]

            if should_cancel and should_cancel():
                for f in futures:
                    f.cancel()
                break

            try:
                found = future.result()
                kept = take_per_keyword(found, per_keyword)
                for row in kept:
                    row.setdefault("keyword", keyword)
                rows.extend(kept)
                report.append({
                    "keyword": keyword, "found": len(found), "kept": len(kept), "status": "ok",
                })
            except Exception as exc:
                report.append({
                    "keyword": keyword, "found": 0, "kept": 0,
                    "status": "failed", "error": str(exc)[:200],
                })

            if on_progress:
                on_progress(len(report), len(keywords), len(rows))

    return rows, report


def fetch_post_content(client, urls: list, on_progress=None, should_cancel=None,
                       concurrency: int = DEFAULT_CONCURRENCY) -> list:
    """Fetch full post content for a list of post URLs, in concurrent chunks."""
    chunks = [urls[i:i + CONTENT_CHUNK_SIZE] for i in range(0, len(urls), CONTENT_CHUNK_SIZE)]
    posts = []

    def fetch(chunk):
        run = client.actor(PROFILE_ACTOR).call(
            run_input={
                "targetUrls": chunk, "maxPosts": 1,
                "maxReactions": 0, "postNestedReactions": False,
                "maxComments": 0, "postNestedComments": False,
            },
            logger=None,
        )
        if run is None or not is_successful(_status_of(run)):
            raise ActorError(f"content run finished as {_status_of(run) or 'UNKNOWN'}")
        return list(client.dataset(get_dataset_id(run)).iterate_items())

    with ThreadPoolExecutor(max_workers=min(concurrency, max(len(chunks), 1))) as pool:
        futures = [pool.submit(fetch, c) for c in chunks]
        for done, future in enumerate(as_completed(futures), start=1):
            if should_cancel and should_cancel():
                for f in futures:
                    f.cancel()
                break
            try:
                posts.extend(future.result())
            except Exception:
                pass  # a failed chunk loses those posts, not the whole sweep
            if on_progress:
                on_progress(done, len(chunks), len(posts))

    return posts
