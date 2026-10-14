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
ARCHIVE_TAB = "Archive"
PICKS_TAB = "今日精選"

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
    """Keys of every job already in the sheet, so a run only adds new jobs."""
    keys: set[str] = set()
    for title, columns, first in ((JOBS_TAB, JOBS_COLUMNS, True), (FILTERED_TAB, FILTERED_COLUMNS, False)):
        ws = _tab(book, title, columns, first)
        header = ws.row_values(1)
        keys.update(k for k in ws.col_values(header.index("key") + 1)[1:] if k)
    archive = {ws.title: ws for ws in book.worksheets()}.get(ARCHIVE_TAB)
    if archive is not None and "key" in archive.row_values(1):
        keys.update(k for k in archive.col_values(archive.row_values(1).index("key") + 1)[1:] if k)
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


def append_tab(book: gspread.Spreadsheet, title: str, df: pd.DataFrame, today: str) -> bool:
    """Add one run's rows to the Jobs or Filtered out tab. One tab per call, so a
    retry after a failure never adds the other tab's rows twice."""
    if df.empty:
        return True
    columns, first = {JOBS_TAB: (JOBS_COLUMNS, True), FILTERED_TAB: (FILTERED_COLUMNS, False)}[title]
    ws = _tab(book, title, columns, first)
    # Follow the sheet's own column order, so you can rearrange or add columns.
    header = ws.row_values(1)
    ws.append_rows(_rows(df, header, today), value_input_option="RAW")
    return True


def append_run(book: gspread.Spreadsheet, shortlist: pd.DataFrame, excluded: pd.DataFrame, today: str) -> None:
    append_tab(book, JOBS_TAB, shortlist, today)
    append_tab(book, FILTERED_TAB, excluded, today)


RUNLOG_TAB = "Run log"
RUNLOG_COLUMNS = ["run_time", "status", "minutes", "scraped", "new_jobs", "scored", "failed_steps",
                  "retried_ok", "details", "per_site", "run_url"]


def append_runlog(book: gspread.Spreadsheet, row: dict) -> bool:
    """One row per run: what ran, what failed after retries, and a link to the full log."""
    ws = _tab(book, RUNLOG_TAB, RUNLOG_COLUMNS)
    header = ws.row_values(1)
    ws.append_rows([[_cell(row.get(c, "")) for c in header]], value_input_option="RAW")
    return True


# ---------- tidy-up: status dropdown, colours, today's picks, archive ----------

STATUSES = ["想投", "已投", "面試", "Offer", "拒絕", "不投"]
STATUS_COLORS = {  # row background per status
    "想投": "#FFF2CC", "已投": "#CFE2F3", "面試": "#D9EAD3",
    "Offer": "#B6D7A8", "拒絕": "#EFEFEF", "不投": "#EFEFEF",
}


def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def setup_jobs_tab(book: gspread.Spreadsheet) -> None:
    """Status dropdown and row colours on the Jobs tab. Runs once: skipped when
    the tab already has conditional formats, so your own edits to them stay."""
    meta = book.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId,title),conditionalFormats)"})
    sheet = next((s for s in meta["sheets"] if s["properties"]["title"] == JOBS_TAB), None)
    if sheet is None or sheet.get("conditionalFormats"):
        return
    sid = sheet["properties"]["sheetId"]
    header = book.worksheet(JOBS_TAB).row_values(1)
    status_col = header.index("status")
    status_letter = _col_letter(status_col + 1)
    score_col = header.index("fit_score")
    all_rows = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(header)}

    requests = [{
        "setDataValidation": {
            "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in STATUSES]},
                "showCustomUi": True,
                "strict": False,
            },
        }
    }]
    for i, (status, color) in enumerate(STATUS_COLORS.items()):
        fmt = {"backgroundColor": _rgb(color)}
        if status in ("拒絕", "不投"):
            fmt["textFormat"] = {"foregroundColor": _rgb("#999999")}
        requests.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [all_rows],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                          "values": [{"userEnteredValue": f'=${status_letter}2="{status}"'}]},
                            "format": fmt},
        }}})
    # Scores of 8+ in bold green.
    requests.append({"addConditionalFormatRule": {"index": len(STATUS_COLORS), "rule": {
        "ranges": [{"sheetId": sid, "startRowIndex": 1, "startColumnIndex": score_col, "endColumnIndex": score_col + 1}],
        "booleanRule": {"condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "8"}]},
                        "format": {"textFormat": {"bold": True, "foregroundColor": _rgb("#38761D")}}},
    }}})
    book.batch_update({"requests": requests})


PICKS_COLUMNS = ["fit_score", "summary_zh", "title", "company", "location", "salary",
                 "ai_sponsorship", "h1b_approvals", "date_found", "job_url"]


def ensure_picks_tab(book: gspread.Spreadsheet, min_score: int, days: int) -> None:
    """A tab whose formula lists recent, high-scoring jobs with no status yet.
    Created once; it updates itself as rows are added or given a status."""
    if PICKS_TAB in {ws.title for ws in book.worksheets()}:
        return
    header = book.worksheet(JOBS_TAB).row_values(1)
    col = {c: f"{JOBS_TAB}!{_col_letter(header.index(c) + 1)}2:{_col_letter(header.index(c) + 1)}"
           for c in PICKS_COLUMNS + ["status"]}
    columns = ", ".join(col[c] for c in PICKS_COLUMNS)
    formula = (
        f'=IFERROR(SORT(FILTER({{{columns}}}, '
        f'{col["fit_score"]}>={min_score}, {col["status"]}="", '
        f'{col["date_found"]}>=TEXT(TODAY()-{days},"yyyy-mm-dd")), 1, FALSE), '
        f'"目前沒有符合條件的職缺")'
    )
    ws = book.add_worksheet(title=PICKS_TAB, rows=500, cols=len(PICKS_COLUMNS), index=0)
    ws.update(range_name="A1", values=[PICKS_COLUMNS, [formula]], value_input_option="USER_ENTERED")
    ws.freeze(rows=1)


def archive_old(book: gspread.Spreadsheet, today: str, days: int) -> int:
    """Move Jobs rows older than `days` that still have no status to the Archive tab.
    Rows you gave a status are never moved. Returns how many rows moved."""
    jobs = book.worksheet(JOBS_TAB)
    rows = jobs.get_all_values()
    if len(rows) < 2:
        return 0
    header = rows[0]
    i_date, i_status = header.index("date_found"), header.index("status")
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    old = [(n, r) for n, r in enumerate(rows[1:], start=1)
           if len(r) > i_status and r[i_date] and r[i_date] < cutoff and not r[i_status].strip()]
    if not old:
        return 0
    archive = _tab(book, ARCHIVE_TAB, header)
    archive.append_rows([r for _, r in old], value_input_option="RAW")
    # Delete bottom-up so earlier row numbers stay valid.
    book.batch_update({"requests": [
        {"deleteDimension": {"range": {"sheetId": jobs.id, "dimension": "ROWS",
                                       "startIndex": n, "endIndex": n + 1}}}
        for n, _ in sorted(old, reverse=True)
    ]})
    return len(old)


def tidy(book: gspread.Spreadsheet, today: str, cfg: dict, warn=print) -> None:
    """Optional sheet niceties. A failure here is reported but never stops the run."""
    steps = [
        ("status dropdown and colours", lambda: setup_jobs_tab(book)),
        ("today's picks tab", lambda: ensure_picks_tab(book, cfg.get("picks_min_score", 7), cfg.get("picks_days", 3))),
        ("archive", lambda: print(f"Archived {archive_old(book, today, cfg.get('archive_after_days', 14))} old rows")),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as e:  # formatting must never cost a day's jobs
            warn(f"Google Sheet {name} skipped: {e}")
