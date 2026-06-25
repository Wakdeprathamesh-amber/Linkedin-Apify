"""
LinkedIn Profile Posts Scraper
Uses Apify's harvestapi/linkedin-profile-posts actor (No Cookies required)
"""

import os
import json
import argparse
from apify_client import ApifyClient


ACTOR_ID = "harvestapi/linkedin-profile-posts"


def format_post(post: dict, profile_url: str) -> str:
    """Format a single post for display."""
    lines = []
    lines.append("=" * 70)

    # Author info
    author = post.get("author", {}) or {}
    author_name = author.get("name") or post.get("authorName") or "Unknown"
    lines.append(f"👤 Author : {author_name}")
    lines.append(f"🔗 Profile: {profile_url}")

    # Post timestamp
    posted_at = (
        post.get("postedAt")
        or post.get("createdAt")
        or post.get("publishedAt")
        or ""
    )
    if posted_at:
        lines.append(f"📅 Posted : {posted_at}")

    # Post URL
    post_url = (
        post.get("url")
        or post.get("postUrl")
        or post.get("link")
        or ""
    )
    if post_url:
        lines.append(f"🌐 Post URL: {post_url}")

    # Post content/text
    text = (
        post.get("text")
        or post.get("content")
        or post.get("description")
        or ""
    )
    if text:
        if len(text) > 500:
            text = text[:497] + "..."
        lines.append(f"\n📝 Content:\n{text}")

    # Engagement stats
    reactions = (
        post.get("reactionsCount")
        or post.get("likesCount")
        or post.get("reactions")
        or 0
    )
    comments = post.get("commentsCount") or post.get("comments") or 0
    shares = post.get("sharesCount") or post.get("shares") or 0
    if any([reactions, comments, shares]):
        lines.append(
            f"\n📊 Engagement: 👍 {reactions} reactions  "
            f"💬 {comments} comments  🔁 {shares} shares"
        )

    lines.append("=" * 70)
    return "\n".join(lines)


def scrape_profiles(
    profile_urls: list,
    api_token: str,
    max_posts: int = 5,
    output_json: str = None,
) -> list:
    """
    Scrape latest posts from a list of LinkedIn profile URLs.

    Args:
        profile_urls: List of LinkedIn profile/company URLs
        api_token:    Apify API token
        max_posts:    Max posts to fetch per profile
        output_json:  Optional path to save raw JSON results

    Returns:
        List of post dicts from Apify dataset
    """
    client = ApifyClient(api_token)

    run_input = {
        "targetUrls": profile_urls,
        "maxPosts": max_posts,
        "maxReactions": 0,
        "postNestedReactions": False,
        "maxComments": 0,
        "postNestedComments": False,
    }

    print(f"\n🚀 Starting Apify actor: {ACTOR_ID}")
    print(f"   Profiles  : {len(profile_urls)}")
    print(f"   Max posts : {max_posts} per profile")
    print("   Waiting for results...\n")

    run = client.actor(ACTOR_ID).call(run_input=run_input)
    # newer apify-client returns a Pydantic model; support both styles
    dataset_id = (
        run.default_dataset_id
        if hasattr(run, "default_dataset_id")
        else run["defaultDatasetId"]
    )
    print(
        f"✅ Run complete. Dataset: "
        f"https://console.apify.com/storage/datasets/{dataset_id}\n"
    )

    posts = list(client.dataset(dataset_id).iterate_items())

    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(posts, f, indent=2, ensure_ascii=False)
        print(f"💾 Raw JSON saved to: {output_json}\n")

    return posts


def group_posts_by_profile(posts: list, profile_urls: list) -> dict:
    """Group posts back to their source profile URL."""
    grouped = {url: [] for url in profile_urls}

    for post in posts:
        author_url = (
            post.get("authorUrl")
            or post.get("profileUrl")
            or (post.get("author") or {}).get("url")
            or (post.get("author") or {}).get("profileUrl")
            or ""
        ).rstrip("/").lower()

        matched = False
        for url in profile_urls:
            norm_url = url.rstrip("/").lower()
            if author_url in norm_url or norm_url in author_url:
                grouped[url].append(post)
                matched = True
                break

        # Fallback: first profile if we can't match
        if not matched and profile_urls:
            grouped[profile_urls[0]].append(post)

    return grouped


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape latest LinkedIn posts from one or more profiles "
            "using Apify (no cookies required)."
        )
    )
    parser.add_argument(
        "profiles",
        nargs="*",
        help="LinkedIn profile/company URLs",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("APIFY_API_TOKEN"),
        help="Apify API token (or set APIFY_API_TOKEN env var)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=5,
        help="Max posts per profile (default: 5)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Save raw results as JSON (e.g. results.json)",
    )
    parser.add_argument(
        "--profiles-file",
        default=None,
        help="Text file with one LinkedIn URL per line",
    )

    args = parser.parse_args()

    # ── Collect URLs ──────────────────────────────────────────────────────
    profile_urls = list(args.profiles)

    if args.profiles_file:
        with open(args.profiles_file, "r", encoding="utf-8") as f:
            file_urls = [
                line.strip()
                for line in f
                if line.strip() and not line.startswith("#")
            ]
        profile_urls.extend(file_urls)

    # Interactive mode when nothing is supplied
    if not profile_urls:
        print("LinkedIn Profile Posts Scraper")
        print("Enter LinkedIn profile URLs (one per line, blank line when done):\n")
        while True:
            url = input("  URL: ").strip()
            if not url:
                break
            profile_urls.append(url)

    if not profile_urls:
        print("❌ No profile URLs provided. Exiting.")
        return

    # ── Validate token ────────────────────────────────────────────────────
    if not args.token:
        print("❌ No Apify API token found.")
        print(
            "   Set the APIFY_API_TOKEN environment variable "
            "or pass --token <YOUR_TOKEN>"
        )
        return

    # ── Scrape ────────────────────────────────────────────────────────────
    posts = scrape_profiles(
        profile_urls=profile_urls,
        api_token=args.token,
        max_posts=args.max_posts,
        output_json=args.output,
    )

    if not posts:
        print(
            "⚠️  No posts returned. "
            "The profiles may be private or have no recent activity."
        )
        return

    # ── Display ───────────────────────────────────────────────────────────
    grouped = group_posts_by_profile(posts, profile_urls)

    print(
        f"\n📋 Results — {len(posts)} post(s) across "
        f"{len(profile_urls)} profile(s)\n"
    )

    for profile_url in profile_urls:
        profile_posts = grouped.get(profile_url, [])
        print(f"\n{'━' * 70}")
        print(f"🔍 Profile: {profile_url}")
        print(f"   Found {len(profile_posts)} post(s)")
        print(f"{'━' * 70}")

        if not profile_posts:
            print("   ⚠️  No posts found for this profile.")
            continue

        for i, post in enumerate(profile_posts, 1):
            print(f"\n  Post #{i}")
            print(format_post(post, profile_url))

    print(f"\n✅ Done. Total posts fetched: {len(posts)}")


if __name__ == "__main__":
    main()
