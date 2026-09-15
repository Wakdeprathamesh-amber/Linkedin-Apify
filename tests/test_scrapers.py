"""Tests for the pure logic in scrapers.py.

Fixtures are real Apify payloads captured from live runs, so these tests cost
nothing and need no network.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrapers  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


# ─── Keyword matching (issue #3: "cas" matched "Newcastle" / "forecast") ─────

def test_short_keyword_does_not_match_inside_a_word():
    """The live bug: 'cas' tagged 20 posts via forecast/Newcastle/showcase."""
    patterns = scrapers.compile_keyword_patterns(["cas"])
    for text in ["Our forecast for 2026", "Newcastle University", "a showcase event", "the case study"]:
        assert scrapers.match_keywords(text, patterns) == [], f"'cas' wrongly matched {text!r}"


def test_short_keyword_still_matches_as_a_whole_word():
    patterns = scrapers.compile_keyword_patterns(["cas"])
    assert scrapers.match_keywords("Applying for a CAS this year", patterns) == ["cas"]
    assert scrapers.match_keywords("no CAS, no visa.", patterns) == ["cas"]


def test_multiword_keyword_matches():
    patterns = scrapers.compile_keyword_patterns(["higher education", "student housing"])
    found = scrapers.match_keywords("The higher education sector and student housing market", patterns)
    assert found == ["higher education", "student housing"]


def test_matching_is_case_insensitive():
    patterns = scrapers.compile_keyword_patterns(["Student Accommodation"])
    assert scrapers.match_keywords("STUDENT ACCOMMODATION demand", patterns) == ["Student Accommodation"]


def test_keyword_with_regex_special_characters_is_escaped():
    patterns = scrapers.compile_keyword_patterns(["c++ (beta)"])
    assert scrapers.match_keywords("we use c++ (beta) here", patterns) == ["c++ (beta)"]
    assert scrapers.match_keywords("we use cxx beta here", patterns) == []


def test_real_fixture_no_longer_produces_false_positives():
    """Regression against the actual posts that were mistagged in production."""
    posts = load("profile_posts.json")
    patterns = scrapers.compile_keyword_patterns(["cas"])
    for post in posts:
        text = post.get("content") or ""
        if "cas" in text.lower() and "cas " not in text.lower():
            assert scrapers.match_keywords(text, patterns) == []


# ─── URL normalisation and dedupe (issue #4) ────────────────────────────────

def test_normalize_strips_query_and_trailing_slash():
    a = scrapers.normalize_post_url("https://www.linkedin.com/posts/foo-activity-123/?utm_source=x")
    b = scrapers.normalize_post_url("https://www.linkedin.com/posts/foo-activity-123")
    assert a == b


def test_normalize_is_case_insensitive_on_host_only():
    a = scrapers.normalize_post_url("HTTPS://WWW.LinkedIn.com/posts/Foo-Activity-123")
    assert a.startswith("https://www.linkedin.com/")
    assert "Foo-Activity-123" in a


def test_normalize_handles_empty_and_none():
    assert scrapers.normalize_post_url("") == ""
    assert scrapers.normalize_post_url(None) == ""


def test_dedupe_keeps_one_row_per_url():
    rows = [
        {"post_url": "https://www.linkedin.com/posts/a-activity-1", "keyword": "one"},
        {"post_url": "https://www.linkedin.com/posts/a-activity-1?x=1", "keyword": "two"},
        {"post_url": "https://www.linkedin.com/posts/b-activity-2", "keyword": "three"},
    ]
    unique, extras = scrapers.dedupe_keyword_results(rows)
    assert len(unique) == 2
    assert unique[0]["keyword"] == "one"


def test_dedupe_preserves_other_keywords_that_found_the_same_post():
    rows = [
        {"post_url": "https://www.linkedin.com/posts/a-activity-1", "keyword": "one"},
        {"post_url": "https://www.linkedin.com/posts/a-activity-1", "keyword": "two"},
    ]
    unique, extras = scrapers.dedupe_keyword_results(rows)
    key = scrapers.normalize_post_url("https://www.linkedin.com/posts/a-activity-1")
    assert extras[key] == ["one", "two"]


def test_dedupe_drops_rows_without_a_url():
    rows = [{"post_url": "", "keyword": "one"}, {"keyword": "two"}]
    unique, _ = scrapers.dedupe_keyword_results(rows)
    assert unique == []


def test_dedupe_on_real_fixture():
    rows = load("keyword_urls.json")
    unique, _ = scrapers.dedupe_keyword_results(rows)
    assert len(unique) == len({scrapers.normalize_post_url(r["post_url"]) for r in rows})


# ─── Budget guard ───────────────────────────────────────────────────────────

def test_estimate_scales_with_keyword_count_not_depth():
    """Stage 1 is charged per run, so depth must not change the stage-1 term."""
    shallow = scrapers.estimate_sweep_cost(n_keywords=100, per_keyword=5)
    deep = scrapers.estimate_sweep_cost(n_keywords=100, per_keyword=20)
    assert deep > shallow                      # stage 2 grows with depth
    assert shallow >= 100 * scrapers.KEYWORD_RUN_COST_USD


def test_estimate_matches_measured_full_sweep():
    """295 keywords at 10 deep measured ~$16.60 against live pricing."""
    assert 14.0 < scrapers.estimate_sweep_cost(n_keywords=295, per_keyword=10) < 19.0


def test_budget_blocks_when_estimate_exceeds_remaining():
    verdict = scrapers.evaluate_budget(estimated=16.60, used=25.0, cap=29.0)
    assert verdict["ok"] is False
    assert verdict["remaining"] == pytest.approx(4.0)


def test_budget_allows_when_affordable():
    verdict = scrapers.evaluate_budget(estimated=16.60, used=7.15, cap=29.0)
    assert verdict["ok"] is True


def test_budget_keeps_a_safety_margin():
    """An estimate that would consume >90% of what is left must be refused."""
    verdict = scrapers.evaluate_budget(estimated=9.9, used=19.0, cap=29.0)
    assert verdict["ok"] is False


# ─── Actor run status (issue #5) ────────────────────────────────────────────

@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED", "ABORTING", "", None])
def test_non_succeeded_statuses_are_failures(status):
    assert scrapers.is_successful(status) is False


def test_succeeded_is_the_only_success():
    assert scrapers.is_successful("SUCCEEDED") is True


# ─── clean_post against a real payload ──────────────────────────────────────

def test_clean_post_on_real_payload():
    posts = load("profile_posts.json")
    for post in posts:
        cleaned = scrapers.clean_post(post)
        assert cleaned["authorName"] != "Unknown"
        assert cleaned["text"]
        assert cleaned["timestampMs"] > 0
        assert cleaned["postUrl"].startswith("https://")


def test_clean_post_survives_an_empty_payload():
    cleaned = scrapers.clean_post({})
    assert cleaned["authorName"] == "Unknown"
    assert cleaned["timestampMs"] == 0
    assert cleaned["reactionsCount"] == 0


def test_truncate_to_requested_depth():
    rows = [{"post_url": f"https://x/{i}", "keyword": "k"} for i in range(20)]
    assert len(scrapers.take_per_keyword(rows, 10)) == 10
    assert len(scrapers.take_per_keyword(rows, 50)) == 20


# ─── Actor constraints learned from live runs ───────────────────────────────

def test_keyword_run_timeout_respects_the_actors_minimum():
    """The actor fails with "Low timeout! 300 sec is the minimum." below 300s.
    A smoke test caught this failing every keyword in the sweep."""
    assert scrapers.KEYWORD_RUN_TIMEOUT_S >= 300


def test_per_run_charge_cap_meets_apifys_floor():
    """Apify rejects a per-run cap below $0.70 with max-total-charge-usd-below-minimum."""
    assert float(scrapers.MAX_CHARGE_PER_RUN_USD) >= 0.70


def test_concurrency_stays_under_the_account_limit():
    """The account allows 32 concurrent jobs and is shared with another project."""
    assert scrapers.DEFAULT_CONCURRENCY <= 16
