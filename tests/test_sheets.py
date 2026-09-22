"""Tests for the pure sheet logic in sheets.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheets  # noqa: E402


# ─── Archive emptiness (issue #2: headers were never written) ───────────────

def test_a_sheet_of_blank_rows_counts_as_empty():
    """The live bug: get_all_values() returned [['']], len 1 > 0, so the code
    decided the archive already had data and skipped writing the header.
    Both production archive tabs now hold 644 unlabelled rows because of this."""
    assert sheets.is_effectively_empty([[""]]) is True
    assert sheets.is_effectively_empty([["", "", ""]]) is True
    assert sheets.is_effectively_empty([[" ", "\t"]]) is True
    assert sheets.is_effectively_empty([]) is True
    assert sheets.is_effectively_empty([[""], [""]]) is True


def test_a_sheet_with_real_content_is_not_empty():
    assert sheets.is_effectively_empty([["authorName", "postedAt"]]) is False
    assert sheets.is_effectively_empty([[""], ["Sachin Doshi"]]) is False


# ─── Row building ───────────────────────────────────────────────────────────

def test_build_block_puts_header_first_and_tags_every_row():
    rows = [{"authorName": "A", "postUrl": "u1"}, {"authorName": "B", "postUrl": "u2"}]
    block = sheets.build_rows_block(rows, "2026-09-15")
    assert block[0] == ["authorName", "postUrl", "scrapeDate"]
    assert block[1] == ["A", "u1", "2026-09-15"]
    assert len(block) == 3


def test_build_block_handles_no_rows():
    block = sheets.build_rows_block([], "2026-09-15")
    assert block == [["No data"]]


def test_build_block_does_not_duplicate_an_existing_scrapedate():
    rows = [{"authorName": "A", "scrapeDate": "old"}]
    block = sheets.build_rows_block(rows, "2026-09-15")
    assert block[0].count("scrapeDate") == 1
    assert block[1][-1] == "2026-09-15"


def test_build_block_fills_missing_keys_from_the_first_row():
    rows = [{"a": 1, "b": 2}, {"a": 3}]
    block = sheets.build_rows_block(rows, "2026-09-15")
    assert block[2] == ["3", "", "2026-09-15"]


# ─── Append-time dedupe (issue #4: 22 duplicate URLs in the archive) ────────

def test_append_skips_rows_already_present():
    existing = [
        ["authorName", "postUrl"],
        ["A", "https://www.linkedin.com/posts/a-activity-1"],
    ]
    new = [
        {"authorName": "A", "postUrl": "https://www.linkedin.com/posts/a-activity-1"},
        {"authorName": "B", "postUrl": "https://www.linkedin.com/posts/b-activity-2"},
    ]
    kept = sheets.filter_already_archived(existing, new, url_field="postUrl")
    assert len(kept) == 1
    assert kept[0]["authorName"] == "B"


def test_append_dedupe_ignores_url_query_strings():
    existing = [["postUrl"], ["https://www.linkedin.com/posts/a-activity-1"]]
    new = [{"postUrl": "https://www.linkedin.com/posts/a-activity-1?utm=x"}]
    assert sheets.filter_already_archived(existing, new, url_field="postUrl") == []


def test_append_dedupe_copes_with_a_headerless_archive():
    """The production archive tabs have no header row at all."""
    existing = [["", "", ""], ["A", "2026-01-01", "https://www.linkedin.com/posts/a-activity-1"]]
    new = [{"postUrl": "https://www.linkedin.com/posts/a-activity-1"}]
    assert sheets.filter_already_archived(existing, new, url_field="postUrl") == []


def test_append_keeps_everything_when_archive_is_empty():
    new = [{"postUrl": "https://x/1"}, {"postUrl": "https://x/2"}]
    assert len(sheets.filter_already_archived([], new, url_field="postUrl")) == 2


# ─── Keyword report (issue #6) ──────────────────────────────────────────────

def test_report_marks_zero_result_keywords_as_duds():
    report = sheets.build_keyword_report([
        {"keyword": "student housing", "found": 12, "kept": 10, "status": "ok"},
        {"keyword": "sh market", "found": 0, "kept": 0, "status": "ok"},
        {"keyword": "btr", "found": 0, "kept": 0, "status": "failed", "error": "timeout"},
    ], "2026-09-15")
    assert report[0] == ["keyword", "found", "kept", "status", "error", "lastRun"]
    assert report[2][3] == "no results"
    assert report[3][3] == "failed"
    assert report[3][4] == "timeout"


# ─── Archive column alignment ───────────────────────────────────────────────
# The row shape changed once already: `alsoMatchedKeywords` was added after
# `keyword`, so a positional append pushed scrapeDate one column to the right
# for every new row while older rows stayed put.

LEGACY_HEADER = [
    "authorName", "postedAt", "text", "postUrl",
    "reactions", "comments", "shares", "keyword", "scrapeDate",
]
CURRENT_HEADER = [
    "authorName", "postedAt", "text", "postUrl",
    "reactions", "comments", "shares", "keyword", "alsoMatchedKeywords", "scrapeDate",
]


def test_new_column_is_appended_without_moving_existing_ones():
    header, _ = sheets.align_rows_to_header(LEGACY_HEADER, CURRENT_HEADER, [])
    # Existing columns keep their index, so rows already archived stay valid.
    assert header[:9] == LEGACY_HEADER
    assert header[9] == "alsoMatchedKeywords"


def test_scrapedate_lands_under_scrapedate_not_under_the_new_column():
    """The actual drift: scrapeDate is index 9 incoming but index 8 in the
    archive. A positional append filed it under alsoMatchedKeywords."""
    row = ["Ada", "2d", "text", "https://x/1", "5", "1", "0", "btr", "cas, sh", "2026-09-22"]
    header, aligned = sheets.align_rows_to_header(LEGACY_HEADER, CURRENT_HEADER, [row])
    out = dict(zip(header, aligned[0]))
    assert out["scrapeDate"] == "2026-09-22"
    assert out["alsoMatchedKeywords"] == "cas, sh"
    assert out["keyword"] == "btr"


def test_missing_column_becomes_blank_not_a_shift():
    """A row written under the legacy header must not slide scrapeDate left."""
    row = ["Ada", "2d", "text", "https://x/1", "5", "1", "0", "btr", "2026-09-22"]
    header, aligned = sheets.align_rows_to_header(CURRENT_HEADER, LEGACY_HEADER, [row])
    out = dict(zip(header, aligned[0]))
    assert out["scrapeDate"] == "2026-09-22"
    assert out["alsoMatchedKeywords"] == ""


def test_short_row_does_not_raise():
    header, aligned = sheets.align_rows_to_header(
        LEGACY_HEADER, CURRENT_HEADER, [["Ada", "2d"]]
    )
    assert len(aligned[0]) == len(header)
    assert aligned[0][-1] == ""


def test_identical_headers_are_left_alone():
    row = ["Ada", "2d", "text", "https://x/1", "5", "1", "0", "btr", "", "2026-09-22"]
    header, aligned = sheets.align_rows_to_header(CURRENT_HEADER, CURRENT_HEADER, [row])
    assert header == CURRENT_HEADER
    assert aligned[0] == row


def test_headerless_legacy_archive_is_detected():
    """A data row must not be mistaken for a header, or rows get aligned
    against garbage. Detection is by overlap with known column names."""
    data_row = ["Ada", "2d", "some post text", "https://x/1", "5", "1", "0", "btr", "2026-09-01"]
    assert sheets.looks_like_header(data_row, CURRENT_HEADER) is False
    assert sheets.looks_like_header(LEGACY_HEADER, CURRENT_HEADER) is True
    assert sheets.looks_like_header([], CURRENT_HEADER) is False
