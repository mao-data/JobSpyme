"""Daily job hunt: scrape with JobSpy, drop jobs that won't sponsor H-1B,
score the rest against your resume with Claude, and write an Excel shortlist.

Usage:
    python hunt.py                 # full run
    python hunt.py --no-claude     # scrape + filter only, no Claude usage
    python hunt.py --config other.yaml
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from jobspy import scrape_jobs

from linkedin_details import fill_descriptions as fill_li
from llm import make_llm
from runlog import RunLog, StepFailed, jobspy_errors

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "data" / "seen_jobs.csv"
OUTPUT_DIR = HERE / "output"

# Retries (3 tries per step) and the record of what failed, shared by the whole run.
RUN = RunLog()


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# Phrases meaning the employer will not sponsor. Checked before the positive ones,
# so "sponsorship is not available" never counts as a yes.
NO_SPONSOR_PATTERNS = [
    r"(?:not|unable to|cannot|can ?not|can't|won't|will not|does not|do not|doesn't|don't|is not able to|are not able to)\s+(?:currently\s+|now\s+|at this time\s+)?(?:offer\s+|provide\s+)?(?:any\s+)?(?:visa\s+|immigration\s+|employment\s+)?sponsor",
    r"sponsorship\s+(?:is\s+|will\s+)?not\s+(?:be\s+)?(?:available|provided|offered|considered)",
    r"\bno\s+(?:visa\s+|h-?1b\s+|immigration\s+)?sponsorship",
    r"without\s+(?:the\s+need\s+for\s+|requiring\s+|requirement\s+of\s+)?(?:current\s+or\s+future\s+|future\s+)?(?:employer\s+|visa\s+|immigration\s+)?sponsorship",
    r"must\s+be\s+(?:a\s+)?u\.?\s?s\.?\s+citizen",
    r"u\.?\s?s\.?\s+citizenship\s+(?:is\s+)?required",
    r"(?:active|current|secret|top secret|ts/sci)\s+(?:security\s+)?clearance",
    r"(?:green card|gc)\s+holders?\s+only",
    r"citizens?\s+(?:or|and|/)\s+(?:green card holders?|permanent residents?)\s+only",
    r"not\s+eligible\s+for\s+(?:visa\s+)?sponsorship",
]
SPONSOR_PATTERNS = [
    r"(?:visa|h-?1b)\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered)",
    r"(?:will|can|able to|happy to|willing to)\s+sponsor",
    r"h-?1b\s+(?:transfer|sponsorship|visa)",
    r"sponsorship\s+(?:is\s+)?(?:available|provided|offered)",
]


# ---------- setup ----------

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_resume(path: Path) -> str:
    # In the cloud the resume comes from a secret instead of a file in the repo.
    if os.environ.get("RESUME_TEXT"):
        return os.environ["RESUME_TEXT"].strip()
    if not path.exists():
        raise SystemExit(f"Resume not found: {path}. Put your resume there or change resume_path in config.yaml.")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
    return path.read_text(encoding="utf-8").strip()


# ---------- scrape ----------

def get_proxies(cfg: dict) -> list[str]:
    return cfg.get("proxies") or [p.strip() for p in os.environ.get("JOBSPY_PROXIES", "").split(",") if p.strip()]


def scrape(cfg: dict) -> pd.DataFrame:
    frames = []
    proxies = get_proxies(cfg)
    searches = []
    for site, opts in cfg["sites"].items():
        # combine_terms: one OR query per location instead of one search per term,
        # for boards (LinkedIn) that block after many requests.
        terms = ([" OR ".join(f'"{t}"' for t in cfg["search_terms"])]
                 if opts.get("combine_terms") else cfg["search_terms"])
        searches += [(site, opts, term, loc) for term in terms for loc in cfg["locations"]]
    failed_in_a_row: dict[str, int] = {}
    for i, (site, opts, term, loc) in enumerate(searches, 1):
        step = f"{site}: {term} @ {loc}"
        if failed_in_a_row.get(site, 0) >= 3:
            continue  # this site is blocking us today; reported once below
        print(f"[{i}/{len(searches)}] {step}", flush=True)

        def search():
            with jobspy_errors() as errors:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term,
                    location=loc,
                    country_indeed="USA",
                    hours_old=cfg.get("hours_old"),
                    results_wanted=opts.get("results_wanted", 25),
                    fetch_description=opts.get("fetch_description", False),
                    proxies=proxies or None,
                    verbose=0,
                )
            if errors and df.empty:
                raise StepFailed("; ".join(dict.fromkeys(errors)))
            if errors:
                RUN.warn(f"{step}: kept {len(df)} jobs, but the board also returned: {errors[0]}")
            return df

        df = RUN.attempt(step, search, waits=tuple(opts.get("retry_waits", (15, 60))))
        if df is None:
            failed_in_a_row[site] = failed_in_a_row.get(site, 0) + 1
            if failed_in_a_row[site] == 3:
                RUN.warn(f"{site}: 3 searches in a row failed after retries; skipped its remaining searches this run")
            df = pd.DataFrame()
        else:
            failed_in_a_row[site] = 0
        if not df.empty:
            df["search_term"] = term
            frames.append(df)
        RUN.count(f"site_{site}", len(df))
        print(f"    {len(df)} jobs", flush=True)
        time.sleep(opts.get("pause_seconds", cfg.get("pause_seconds", 5)))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------- filters ----------

def job_key(row) -> str:
    def norm(s):
        return re.sub(r"\W+", " ", str(s or "").lower()).strip()
    return f"{norm(row['company'])}|{norm(row['title'])}|{norm(row['location'])}"


def in_target_states(location: str, is_remote, states: list[str]) -> bool:
    codes = {US_STATES.get(s.lower(), s.upper()) for s in states}
    loc = str(location or "")
    if any(re.search(rf"(?:^|,\s*){c}(?:,|$)", loc) for c in codes):
        return True
    if any(name in loc.lower() for name, c in US_STATES.items() if c in codes):
        return True
    # Remote jobs listed only as "US"/"United States" are open to any state.
    return bool(is_remote) and not any(re.search(rf"(?:^|,\s*){c}(?:,|$)", loc) for c in US_STATES.values())


def visa_status(description: str) -> tuple[str, str]:
    """('no_sponsorship' | 'sponsors' | 'unknown', matching snippet)."""
    text = str(description or "")
    lower = text.lower()
    for status, patterns in (("no_sponsorship", NO_SPONSOR_PATTERNS), ("sponsors", SPONSOR_PATTERNS)):
        for p in patterns:
            m = re.search(p, lower)
            if m:
                start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
                return status, "..." + " ".join(text[start:end].split()) + "..."
    return "unknown", ""


def normalize_company(name: str) -> str:
    s = re.sub(r"[^\w\s]", " ", str(name or "").lower())
    s = re.sub(r"\b(the|inc|incorporated|llc|ltd|limited|corp|corporation|co|company|lp|llp|plc|pc|group|holdings|usa|us|america)\b", " ", s)
    return " ".join(s.split())


def load_h1b_sponsors(paths: list[str]) -> dict[str, int]:
    """Company -> total H-1B approvals, from USCIS H-1B Employer Data Hub exports."""
    counts: dict[str, int] = {}
    for p in paths:
        path = (HERE / p) if not Path(p).is_absolute() else Path(p)
        df = None
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                df = pd.read_csv(path, sep=None, engine="python", encoding=enc)
                break
            except (UnicodeError, pd.errors.ParserError):
                continue
        if df is None:
            print(f"Could not read H-1B file {path}")
            continue
        employer_col = next((c for c in df.columns if "employer" in c.lower()), None)
        approval_cols = [c for c in df.columns if "approval" in c.lower()]
        if not employer_col or not approval_cols:
            print(f"H-1B file {path} has no Employer/Approval columns; skipped")
            continue
        approvals = sum(pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce").fillna(0) for c in approval_cols)
        for name, n in zip(df[employer_col].map(normalize_company), approvals):
            if name:
                counts[name] = counts.get(name, 0) + int(n)
    return counts


def apply_filters(df: pd.DataFrame, cfg: dict, seen: set[str], sponsors: dict[str, int]):
    f = cfg.get("filters", {})
    df = df.copy()
    df["key"] = df.apply(job_key, axis=1)
    df = df.drop_duplicates(subset="job_url").drop_duplicates(subset="key")
    df = df[~df["key"].isin(seen)]

    words = [w.lower() for w in f.get("exclude_title_words", [])]
    title_re = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I) if words else None

    def cheap_reason(r) -> str:
        if not in_target_states(r["location"], r.get("is_remote"), cfg["locations"]):
            return "outside target states"
        if title_re and title_re.search(str(r["title"])):
            return "title too senior / excluded word"
        return ""

    df["excluded_reason"] = [cheap_reason(r) for _, r in df.iterrows()] if len(df) else []

    # LinkedIn descriptions are fetched here, slowly, and only for jobs that survived.
    li_opts = cfg.get("sites", {}).get("linkedin", {})
    if li_opts.get("slow_descriptions") and len(df):
        need = df[(df["site"] == "linkedin") & (df["excluded_reason"] == "") & df["description"].isna()]
        if len(need):
            texts = fill_li(list(need["job_url"]), li_opts, get_proxies(cfg), log=RUN)
            df["description"] = df["description"].where(df["description"].notna(), df["job_url"].map(texts))

    status = df["description"].map(visa_status)
    df["visa_rule"] = status.map(lambda t: t[0])
    df["visa_evidence"] = status.map(lambda t: t[1])
    df["h1b_approvals"] = df["company"].map(lambda c: sponsors.get(normalize_company(c))) if sponsors else None
    no_visa = (df["excluded_reason"] == "") & (df["visa_rule"] == "no_sponsorship")
    if f.get("require_sponsorship", True):
        df.loc[no_visa, "excluded_reason"] = "says no visa sponsorship"
    return df[df["excluded_reason"] == ""].copy(), df[df["excluded_reason"] != ""].copy()


# ---------- Claude ----------

SCORE_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "the posting's id"},
        "fit_score": {"type": "integer", "description": "1-10, how well the candidate fits"},
        "sponsorship": {"type": "string", "enum": ["sponsors", "unclear", "no_sponsorship"]},
        "sponsorship_evidence": {"type": "string"},
        "years_required": {"type": "integer", "description": "minimum years of experience asked for; 0 if not stated"},
        "summary_zh": {"type": "string", "description": "one sentence in Traditional Chinese on why it fits or not"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "fit_score", "sponsorship", "sponsorship_evidence", "years_required",
                 "summary_zh", "matched_skills", "missing_skills"],
    "additionalProperties": False,
}
SCORE_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": SCORE_ITEM}},
    "required": ["results"],
    "additionalProperties": False,
}

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "resume_bullets": {"type": "array", "items": {"type": "string"}},
        "cover_letter": {"type": "string"},
    },
    "required": ["resume_bullets", "cover_letter"],
    "additionalProperties": False,
}

SYSTEM = """You help a job seeker in the United States triage job postings.
The candidate is on F-1 STEM OPT and will need H-1B visa sponsorship. Judge each posting
against the resume below.

Scoring guide for fit_score:
- 9-10: role, skills and seniority match closely; strong chance of an interview
- 7-8: good match with one or two gaps
- 5-6: partial match; a stretch
- 1-4: poor match (wrong field, far too senior, or a hard requirement the candidate lacks)
Lower the score when the posting needs much more experience than the resume shows.

For sponsorship: "no_sponsorship" only if the posting says so (no sponsorship, citizens only,
security clearance required); "sponsors" only if it says it sponsors; otherwise "unclear".
Quote the deciding sentence in sponsorship_evidence, or leave it empty.

<resume>
{resume}
</resume>"""

TAILOR_PROMPT = """Write application material for this posting, in English.

resume_bullets: 4-6 resume bullet points the candidate can use for this job. Rephrase and
reorder the candidate's real experience to match the posting's keywords. Never invent
employers, tools, numbers or results that are not in the resume.

cover_letter: a concise cover letter (under 250 words), plain text, no placeholders except
[Hiring Manager] if no name is given. Do not mention visa status.

<posting>
{posting}
</posting>"""


def posting_text(r) -> str:
    return (
        f"Title: {r['title']}\nCompany: {r['company']}\nLocation: {r['location']}\n"
        f"Remote: {r.get('is_remote')}\nURL: {r['job_url']}\n\n{r.get('description') or '(no description)'}"
    )


def score_batch(llm, batch: list[tuple[int, str]]) -> dict[int, dict]:
    postings = "\n\n".join(f'<posting id="{i}">\n{text}\n</posting>' for i, text in batch)
    prompt = f"Evaluate each of these {len(batch)} postings. Return one result per posting id.\n\n{postings}"
    ids = [i for i, _ in batch]

    def ask():
        results = llm.ask(prompt, SCORE_SCHEMA, effort="low")["results"]
        by_id = {r["id"]: r for r in results}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise StepFailed(f"reply left out {len(missing)} of {len(ids)} postings")
        return by_id

    by_id = RUN.attempt(f"Claude scoring, postings {ids[0]}-{ids[-1]}", ask, waits=(20, 60))
    if by_id is None:
        return {i: {"error": "Claude scoring failed after 3 tries"} for i in ids}
    return {i: by_id[i] for i in ids}


def run_claude(df: pd.DataFrame, cfg: dict, resume: str, letters_dir: Path):
    """Returns (scored shortlist, jobs Claude found won't sponsor)."""
    ccfg = cfg["claude"]
    llm = make_llm(ccfg, SYSTEM.replace("{resume}", resume))
    limit = ccfg.get("max_jobs_scored", 150)
    overflow = df.iloc[0:0]
    if len(df) > limit:
        # Jobs past the cap still go to the sheet, unscored: the search only covers
        # recent postings, so a job skipped today would never come back.
        print(f"{len(df)} jobs passed the filters; scoring the {limit} newest (max_jobs_scored).")
        df = df.sort_values("date_posted", ascending=False, na_position="last")
        overflow = df.iloc[limit:].assign(summary_zh="未評分：超過每次評分上限 (max_jobs_scored)")
        df = df.iloc[:limit]
    df = df.reset_index(drop=True)

    size = ccfg.get("batch_size", 8)
    items = [(i, posting_text(r)) for i, r in df.iterrows()]
    batches = [items[k:k + size] for k in range(0, len(items), size)]
    print(f"Scoring {len(items)} jobs in {len(batches)} batches...")
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for part in pool.map(lambda b: score_batch(llm, b), batches):
            results.update(part)
    res = [results[i] for i in df.index]

    df["fit_score"] = [r.get("fit_score") for r in res]
    df["ai_sponsorship"] = [r.get("sponsorship", "") for r in res]
    df["ai_sponsorship_evidence"] = [r.get("sponsorship_evidence", "") for r in res]
    df["years_required"] = [r.get("years_required") for r in res]
    df["summary_zh"] = [r.get("summary_zh") or r.get("error", "") for r in res]
    df["matched_skills"] = [", ".join(r.get("matched_skills", [])) for r in res]
    df["missing_skills"] = [", ".join(r.get("missing_skills", [])) for r in res]

    dropped = df.iloc[0:0]
    if cfg.get("filters", {}).get("require_sponsorship", True):
        no_visa = df["ai_sponsorship"] == "no_sponsorship"
        dropped = df[no_visa].assign(excluded_reason="Claude: no visa sponsorship",
                                     visa_evidence=df.loc[no_visa, "ai_sponsorship_evidence"])
        df = df[~no_visa].copy()

    floor = ccfg.get("drop_score_at_or_below")
    if floor is not None:
        low = df["fit_score"].notna() & (df["fit_score"] <= floor)
        dropped = pd.concat([dropped, df[low].assign(excluded_reason=f"Claude score {floor} or lower")])
        df = df[~low].copy()
        if low.any():
            print(f"{int(low.sum())} jobs scored {floor} or lower; moved to Filtered out")

    top = df[df["fit_score"].fillna(0) >= ccfg.get("tailor_min_score", 8)]
    df["letter_file"] = ""
    df["cover_letter"] = ""
    if not top.empty:
        letters_dir.mkdir(parents=True, exist_ok=True)
        print(f"Writing tailored material for {len(top)} top jobs...")
        for idx, r in top.iterrows():
            t = RUN.attempt(f"Claude cover letter: {r['company']} - {r['title']}",
                            lambda: llm.ask(TAILOR_PROMPT.replace("{posting}", posting_text(r)),
                                            TAILOR_SCHEMA, effort="medium"),
                            waits=(20, 60))
            if t is None:
                continue
            name = re.sub(r"[^\w-]+", "_", f"{r['company']}_{r['title']}")[:80] + ".md"
            (letters_dir / name).write_text(
                f"# {r['title']} - {r['company']}\n\n{r['job_url']}\n\n## Resume bullets\n\n"
                + "\n".join(f"- {b}" for b in t["resume_bullets"])
                + f"\n\n## Cover letter\n\n{t['cover_letter']}\n",
                encoding="utf-8",
            )
            df.at[idx, "letter_file"] = f"letters/{name}"
            df.at[idx, "cover_letter"] = t["cover_letter"]

    print(llm.usage_note())
    RUN.count("scored", int(df["fit_score"].notna().sum()))
    df = pd.concat([df, overflow], ignore_index=True)
    return df.sort_values("fit_score", ascending=False, na_position="last"), dropped


# ---------- output ----------

SHORTLIST_COLS = [
    "fit_score", "summary_zh", "title", "company", "location", "is_remote", "site", "date_posted",
    "min_amount", "max_amount", "interval", "ai_sponsorship", "ai_sponsorship_evidence",
    "visa_rule", "visa_evidence", "h1b_approvals", "years_required", "matched_skills",
    "missing_skills", "letter_file", "job_url", "job_url_direct", "search_term",
]


def write_excel(path: Path, shortlist: pd.DataFrame, excluded: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [c for c in SHORTLIST_COLS if c in shortlist.columns]
    ex_cols = ["excluded_reason", "title", "company", "location", "site", "visa_evidence", "job_url"]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        shortlist[cols].to_excel(xw, sheet_name="Shortlist", index=False)
        excluded[[c for c in ex_cols if c in excluded.columns]].to_excel(xw, sheet_name="Filtered out", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            header = [c.value for c in ws[1]]
            for name, width in (("summary_zh", 50), ("title", 40), ("company", 25), ("job_url", 30),
                                ("ai_sponsorship_evidence", 40), ("visa_evidence", 40), ("location", 22)):
                if name in header:
                    ws.column_dimensions[ws.cell(1, header.index(name) + 1).column_letter].width = width
            if "job_url" in header:
                col = header.index("job_url") + 1
                for row in range(2, ws.max_row + 1):
                    cell = ws.cell(row, col)
                    if cell.value:
                        cell.hyperlink, cell.style = cell.value, "Hyperlink"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--no-claude", action="store_true", help="skip Claude scoring")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    use_claude = cfg.get("claude", {}).get("enabled", True) and not args.no_claude
    resume = read_resume(HERE / cfg["resume_path"]) if use_claude else ""

    seen = set(pd.read_csv(SEEN_FILE)["key"]) if SEEN_FILE.exists() else set()
    sheet_id = cfg.get("google_sheet_id") or os.environ.get("GOOGLE_SHEET_ID")
    book = None
    if sheet_id:
        import sheets
        book = RUN.attempt("Google Sheet: connect", lambda: sheets.connect(sheet_id))
        keys = RUN.attempt("Google Sheet: read existing jobs", lambda: sheets.known_keys(book)) if book else None
        if keys is None:
            # Without the sheet's keys every job would be added again as new.
            finish(None, exit_code=1, note="Stopped: couldn't read the Google Sheet, so nothing was scraped.")
        seen |= keys
    sponsors = load_h1b_sponsors(cfg.get("h1b_sponsor_files") or [])

    raw = scrape(cfg)
    RUN.count("scraped", len(raw))
    if raw.empty:
        finish(book, exit_code=1, note="No jobs found. Boards may be blocking us (see the failures above).")
    shortlist, excluded = apply_filters(raw, cfg, seen, sponsors)
    print(f"Scraped {len(raw)}, new and passing filters: {len(shortlist)}, filtered out: {len(excluded)}")

    today = date.today().isoformat()
    run_dir = OUTPUT_DIR / today
    if use_claude and not shortlist.empty:
        shortlist, dropped = run_claude(shortlist, cfg, resume, run_dir / "letters")
        excluded = pd.concat([excluded, dropped])

    out = run_dir / f"jobs_{today}.xlsx"
    write_excel(out, shortlist, excluded)
    print(f"Excel copy: {out}")
    exit_code = 0
    if book is not None:
        # Without Claude the shortlist is left out, so a later full run still scores it.
        to_add = shortlist if use_claude else shortlist.iloc[0:0]
        ok_jobs = RUN.attempt("Google Sheet: add jobs", lambda: sheets.append_tab(book, sheets.JOBS_TAB, to_add, today))
        ok_filtered = RUN.attempt("Google Sheet: add filtered-out rows",
                                  lambda: sheets.append_tab(book, sheets.FILTERED_TAB, excluded, today))
        if ok_jobs is not None:
            RUN.count("jobs_added", len(to_add))
        if ok_jobs is None or ok_filtered is None:
            exit_code = 1  # GitHub emails you when a run fails
        sheets.tidy(book, today, cfg.get("sheet", {}), warn=RUN.warn)

    # Remember what we looked at, so tomorrow's run only shows new jobs. Without
    # Claude, the shortlist isn't remembered so a later full run still scores it.
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    looked_at = [shortlist, excluded] if use_claude else [excluded]
    new_seen = pd.concat(looked_at)[["key", "title", "company", "job_url"]]
    new_seen["first_seen"] = today
    new_seen.to_csv(SEEN_FILE, mode="a", header=not SEEN_FILE.exists(), index=False)
    finish(book, exit_code)


def finish(book, exit_code: int = 0, note: str = "") -> None:
    """Print the run log, put it on the Actions page and the sheet's Run log tab, and exit."""
    if note:
        RUN.warn(note)
    RUN.print_summary()
    RUN.write_github_summary()
    if book is not None:
        import sheets
        if RUN.attempt("Google Sheet: write run log", lambda: sheets.append_runlog(book, RUN.sheet_row())) is None:
            print("Couldn't write the Run log tab; the summary above is the record.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
