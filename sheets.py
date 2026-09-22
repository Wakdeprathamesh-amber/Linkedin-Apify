"""Google Sheets I/O for the LinkedIn scraper.

Each output type keeps exactly two tabs:
  - "<name>"            → the latest run (overwritten every time)
  - "<name> - Archive"  → all previous runs, appended (accumulates)

Split out of app.py so the sheet logic can be tested without Flask or Apify.
"""
import json
import os
from datetime import datetime

import gspread

from scrapers import normalize_post_url as _normalize_url

GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GSHEET_CREDS = os.environ.get("GOOGLE_CREDS_JSON", "credentials.json")


# ─── Client ─────────────────────────────────────────────────────────────────

def get_gspread_client():
    """Return authenticated gspread client using a service account.

    Resolution order:
      1. GOOGLE_CREDENTIALS_JSON env var (raw JSON) — used on Render/cloud.
      2. credentials.json file on disk — used for local development.
    """
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
    """Read one column, skipping the header row."""
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)
    ws = spreadsheet.worksheet(sheet_name) if sheet_name else spreadsheet.sheet1
    return [v.strip() for v in ws.col_values(col)[1:] if v.strip()]


# ─── Pure helpers (unit-tested) ─────────────────────────────────────────────

def is_effectively_empty(values: list) -> bool:
    """True when a worksheet holds nothing but blank cells.

    gspread returns [['']] for a sheet whose only content is an empty first row.
    The previous `len(get_all_values()) > 0` test read that as "has data" and so
    skipped writing the header — which is why both live archive tabs hold
    hundreds of unlabelled rows starting at row 2.
    """
    return not any(str(cell).strip() for row in (values or []) for cell in row)


def build_rows_block(rows: list, today: str) -> list:
    """Header + data rows, every row tagged with the scrape date."""
    if not rows:
        return [["No data"]]

    headers = [h for h in rows[0].keys() if h != "scrapeDate"]
    headers.append("scrapeDate")

    block = [headers]
    for row in rows:
        block.append([str(row.get(h, "")) for h in headers[:-1]] + [today])
    return block


def filter_already_archived(existing: list, new_rows: list, url_field: str = "postUrl") -> list:
    """Drop rows whose URL is already somewhere in the archive.

    Scans every cell rather than a fixed column, because the live archive tabs
    have no header row and so no reliable column index.
    """
    seen = {
        _normalize_url(cell)
        for row in (existing or [])
        for cell in row
        if isinstance(cell, str) and cell.startswith("http")
    }
    kept = []
    for row in new_rows:
        url = _normalize_url(row.get(url_field, ""))
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        kept.append(row)
    return kept


def build_keyword_report(entries: list, today: str) -> list:
    """Per-keyword outcome table — the evidence base for pruning the list."""
    block = [["keyword", "found", "kept", "status", "error", "lastRun"]]
    for e in entries:
        status = e.get("status", "ok")
        if status == "ok" and not e.get("found"):
            status = "no results"
        block.append([
            e.get("keyword", ""),
            str(e.get("found", 0)),
            str(e.get("kept", 0)),
            status,
            e.get("error", ""),
            today,
        ])
    return block


# ─── Export ─────────────────────────────────────────────────────────────────

def export_to_sheet(sheet_url: str, rows: list, sheet_name: str = None):
    """Roll the current tab into its archive, then overwrite it with fresh data."""
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)

    target_name = sheet_name or "Export"
    archive_name = f"{target_name} - Archive"
    today = datetime.now().strftime("%Y-%m-%d")

    new_data = build_rows_block(rows, today)

    # ── 1. Move the current tab's contents into the central archive ────────
    try:
        current_ws = spreadsheet.worksheet(target_name)
    except Exception:
        current_ws = None

    if current_ws is not None:
        existing = current_ws.get_all_values()
        if not is_effectively_empty(existing) and existing[0] != ["No data"]:
            header, data_rows = existing[0], existing[1:]
            try:
                archive_ws = spreadsheet.worksheet(archive_name)
                archive_values = archive_ws.get_all_values()
            except Exception:
                archive_ws = spreadsheet.add_worksheet(
                    title=archive_name,
                    rows=max(len(data_rows) + 10, 100),
                    cols=max(len(header), 20),
                )
                archive_values = []

            # Skip posts already archived by an earlier run.
            url_idx = header.index("postUrl") if "postUrl" in header else None
            if url_idx is not None:
                as_dicts = [{"postUrl": r[url_idx] if url_idx < len(r) else "", "_row": r}
                            for r in data_rows]
                data_rows = [d["_row"] for d in filter_already_archived(archive_values, as_dicts)]

            if data_rows:
                if is_effectively_empty(archive_values):
                    archive_ws.append_rows([header] + data_rows, value_input_option="RAW")
                else:
                    archive_ws.append_rows(data_rows, value_input_option="RAW")

    # ── 2. Overwrite the current tab with the fresh run ────────────────────
    if current_ws is not None:
        current_ws.clear()
        ws = current_ws
    else:
        ws = spreadsheet.add_worksheet(
            title=target_name, rows=max(len(new_data) + 10, 100), cols=20
        )

    ws.update(values=new_data, range_name="A1")


def write_tab(sheet_url: str, block: list, sheet_name: str):
    """Overwrite a tab with a pre-built block (header included). No archiving."""
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)
    try:
        ws = spreadsheet.worksheet(sheet_name)
        ws.clear()
    except Exception:
        ws = spreadsheet.add_worksheet(
            title=sheet_name, rows=max(len(block) + 10, 100), cols=max(len(block[0]), 10)
        )
    ws.update(values=block, range_name="A1")


def repair_archive_headers(sheet_url: str, header: list, tab_names: list) -> dict:
    """One-time fix for archives written without a header row.

    Inserts `header` at row 1 when the tab starts with a blank row and its
    second row looks like data rather than column names.
    """
    gc = get_gspread_client()
    spreadsheet = gc.open_by_url(sheet_url)
    results = {}
    for name in tab_names:
        try:
            ws = spreadsheet.worksheet(name)
        except Exception:
            results[name] = "missing"
            continue

        values = ws.get_all_values()
        if not values:
            results[name] = "empty"
            continue

        first_row_blank = not any(str(c).strip() for c in values[0])
        already_headed = any(str(c).strip() == header[0] for c in values[0])

        if already_headed:
            results[name] = "already ok"
        elif first_row_blank:
            ws.update(values=[header], range_name="A1")
            results[name] = "header written into blank row 1"
        else:
            ws.insert_row(header, index=1)
            results[name] = "header inserted above data"
    return results
