"""Append each run's jobs to a Google Sheet, and read back which jobs it already has.

Auth: a Google service account whose JSON key is in the GOOGLE_SERVICE_ACCOUNT_JSON
environment variable (or a file named by GOOGLE_APPLICATION_CREDENTIALS). Share the
sheet with the service account's email as an Editor.
"""

from __future__ import annotations

import json
import os

import gspread
import pandas as pd

JOBS_TAB = "Jobs"
FILTERED_TAB = "Filtered out"

JOBS_COLUMNS = [
    "date_found", "fit_score", "summary_zh", "title", "company", "location", "is_remote", "site",
    "date_posted", "salary", "ai_sponsorship", "sponsorship_evidence", "h1b_approvals",
    "years_required", "matched_skills", "missing_skills", "job_url", "job_url_direct",
    "cover_letter", "status", "notes", "key",
]
FILTERED_COLUMNS = ["date_found", "excluded_reason", "title", "company", "location", "site",
                    "visa_evidence", "job_url", "key"]

MAX_CELL = 45000  # Sheets rejects cells over 50,000 characters


def connect(sheet_id: str) -> gspread.Spreadsheet:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        client = gspread.service_account_from_dict(json.loads(raw))
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        client = gspread.service_account(filename=os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    else:
        raise SystemExit("google_sheet_id is set but GOOGLE_SERVICE_ACCOUNT_JSON is not. See README.")
    return client.open_by_key(sheet_id)


def _tab(book: gspread.Spreadsheet, title: str, columns: list[str], first: bool = False) -> gspread.Worksheet:
    """The tab named `title`, created with a header row if missing. With `first`,
    an existing first tab (e.g. the "Untitled" one a new sheet starts with) is renamed."""
    tabs = {ws.title: ws for ws in book.worksheets()}
    if title in tabs:
        ws = tabs[title]
    elif first:
        ws = book.sheet1
        ws.update_title(title)
    else:
        ws = book.add_worksheet(title=title, rows=1000, cols=len(columns))
    if ws.row_values(1) != columns:
        if not ws.row_values(1):
            ws.update(range_name="A1", values=[columns])
            ws.freeze(rows=1)
        else:
            missing = [c for c in columns if c not in ws.row_values(1)]
            if missing:
                raise SystemExit(f"Tab '{title}' is missing columns {missing}. Add them to row 1 or rename the tab.")
    return ws


def known_keys(book: gspread.Spreadsheet) -> set[str]:
    """Keys of every job already in either tab, so a run only adds new jobs."""
    keys: set[str] = set()
    for title, columns, first in ((JOBS_TAB, JOBS_COLUMNS, True), (FILTERED_TAB, FILTERED_COLUMNS, False)):
        ws = _tab(book, title, columns, first)
        header = ws.row_values(1)
        keys.update(k for k in ws.col_values(header.index("key") + 1)[1:] if k)
    return keys


def _cell(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if hasattr(v, "item"):  # numpy scalar -> plain Python, so JSON can carry it
        v = v.item()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float, bool)):
        return v
    return str(v)[:MAX_CELL]


def _salary(r) -> str:
    lo, hi, per = r.get("min_amount"), r.get("max_amount"), r.get("interval")
    if pd.isna(lo) and pd.isna(hi):
        return ""
    fmt = lambda x: "" if pd.isna(x) else f"{x:,.0f}"
    return f"{fmt(lo)}-{fmt(hi)} {per or ''}".strip(" -")


ALIASES = {"sponsorship_evidence": "ai_sponsorship_evidence"}


def _rows(df: pd.DataFrame, header: list[str], today: str) -> list[list]:
    rows = []
    for _, r in df.iterrows():
        values = {"date_found": today, "salary": _salary(r), "status": "", "notes": ""}
        rows.append([_cell(values[c]) if c in values else _cell(r.get(ALIASES.get(c, c))) for c in header])
    return rows


def append_run(book: gspread.Spreadsheet, shortlist: pd.DataFrame, excluded: pd.DataFrame, today: str) -> None:
    for title, columns, first, df in ((JOBS_TAB, JOBS_COLUMNS, True, shortlist),
                                      (FILTERED_TAB, FILTERED_COLUMNS, False, excluded)):
        if df.empty:
            continue
        ws = _tab(book, title, columns, first)
        # Follow the sheet's own column order, so you can rearrange or add columns.
        header = ws.row_values(1)
        ws.append_rows(_rows(df, header, today), value_input_option="RAW")
