"""Daily job hunt: scrape with JobSpy, drop jobs that won't sponsor H-1B,
score the rest against your resume with Claude, and write an Excel shortlist.

Usage:
    python hunt.py                 # full run
    python hunt.py --no-claude     # scrape + filter only, no API cost
    python hunt.py --config other.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import yaml
from jobspy import scrape_jobs

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "data" / "seen_jobs.csv"
OUTPUT_DIR = HERE / "output"

# Opus 5.5 list price, USD per million tokens, for the end-of-run estimate.
PRICE_IN, PRICE_OUT = 4.00, 20.00

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
    if not path.exists():
        raise SystemExit(f"Resume not found: {path}. Put your resume there or change resume_path in config.yaml.")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
    return path.read_text(encoding="utf-8").strip()


# ---------- scrape ----------

def scrape(cfg: dict) -> pd.DataFrame:
    frames = []
    searches = [
        (site, opts, term, loc)
        for site, opts in cfg["sites"].items()
        for term in cfg["search_terms"]
        for loc in cfg["locations"]
    ]
    for i, (site, opts, term, loc) in enumerate(searches, 1):
        print(f"[{i}/{len(searches)}] {site}: {term} @ {loc}", flush=True)
        try:
            df = scrape_jobs(
                site_name=[site],
                search_term=term,
                location=loc,
                country_indeed="USA",
                hours_old=cfg.get("hours_old"),
                results_wanted=opts.get("results_wanted", 25),
                fetch_description=opts.get("fetch_description", False),
                proxies=cfg.get("proxies") or None,
                verbose=0,
            )
        except Exception as e:  # one bad board/search shouldn't kill the run
            print(f"    failed: {e}")
            df = pd.DataFrame()
        if not df.empty:
            df["search_term"] = term
            frames.append(df)
            print(f"    {len(df)} jobs")
        time.sleep(cfg.get("pause_seconds", 5))
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

    status = df["description"].map(visa_status)
    df["visa_rule"] = status.map(lambda t: t[0])
    df["visa_evidence"] = status.map(lambda t: t[1])
    df["h1b_approvals"] = df["company"].map(lambda c: sponsors.get(normalize_company(c))) if sponsors else None

    words = [w.lower() for w in f.get("exclude_title_words", [])]
    title_re = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I) if words else None

    reasons = []
    for _, r in df.iterrows():
        if not in_target_states(r["location"], r.get("is_remote"), cfg["locations"]):
            reasons.append("outside target states")
        elif title_re and title_re.search(str(r["title"])):
            reasons.append("title too senior / excluded word")
        elif f.get("require_sponsorship", True) and r["visa_rule"] == "no_sponsorship":
            reasons.append("says no visa sponsorship")
        else:
            reasons.append("")
    df["excluded_reason"] = reasons
    return df[df["excluded_reason"] == ""].copy(), df[df["excluded_reason"] != ""].copy()


# ---------- Claude ----------

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer", "description": "1-10, how well the candidate fits"},
        "sponsorship": {"type": "string", "enum": ["sponsors", "unclear", "no_sponsorship"]},
        "sponsorship_evidence": {"type": "string"},
        "years_required": {"type": "integer", "description": "minimum years of experience asked for; 0 if not stated"},
        "summary_zh": {"type": "string", "description": "one sentence in Traditional Chinese on why it fits or not"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fit_score", "sponsorship", "sponsorship_evidence", "years_required",
                 "summary_zh", "matched_skills", "missing_skills"],
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
The candidate needs H-1B visa sponsorship. Judge each posting against the resume below.

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


class Claude:
    def __init__(self, cfg: dict, resume: str):
        self.client = anthropic.Anthropic(max_retries=4)
        self.model = cfg["model"]
        self.system = SYSTEM.replace("{resume}", resume)
        self.tokens_in = self.tokens_out = 0

    def _ask(self, prompt: str, schema: dict, effort: str, max_tokens: int) -> dict:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            cache_control={"type": "ephemeral"},
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        u = response.usage
        self.tokens_in += (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        self.tokens_out += u.output_tokens or 0
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this posting")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("response cut off at max_tokens")
        return json.loads(next(b.text for b in response.content if b.type == "text"))

    def score(self, posting: str) -> dict:
        return self._ask(f"Evaluate this posting.\n\n<posting>\n{posting}\n</posting>", SCORE_SCHEMA, "low", 4000)

    def tailor(self, posting: str) -> dict:
        return self._ask(TAILOR_PROMPT.replace("{posting}", posting), TAILOR_SCHEMA, "medium", 8000)

    def cost(self) -> float:
        return self.tokens_in / 1e6 * PRICE_IN + self.tokens_out / 1e6 * PRICE_OUT


def posting_text(r) -> str:
    return (
        f"Title: {r['title']}\nCompany: {r['company']}\nLocation: {r['location']}\n"
        f"Remote: {r.get('is_remote')}\nURL: {r['job_url']}\n\n{r.get('description') or '(no description)'}"
    )


def run_claude(df: pd.DataFrame, cfg: dict, resume: str, letters_dir: Path):
    """Returns (scored shortlist, jobs Claude found won't sponsor)."""
    ccfg = cfg["claude"]
    claude = Claude(ccfg, resume)
    limit = ccfg.get("max_jobs_scored", 150)
    if len(df) > limit:
        # The rest stay unseen, so the next run picks them up.
        print(f"{len(df)} jobs passed the filters; scoring the {limit} newest (max_jobs_scored).")
        df = df.sort_values("date_posted", ascending=False, na_position="last").head(limit)
    df = df.copy()
    rows = [r for _, r in df.iterrows()]

    def score_one(r):
        try:
            return claude.score(posting_text(r))
        except (anthropic.APIError, RuntimeError, json.JSONDecodeError, StopIteration) as e:
            return {"error": str(e)}

    print(f"Scoring {len(rows)} jobs with {claude.model}...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(score_one, rows))

    df["fit_score"] = [res.get("fit_score") for res in results]
    df["ai_sponsorship"] = [res.get("sponsorship", "") for res in results]
    df["ai_sponsorship_evidence"] = [res.get("sponsorship_evidence", "") for res in results]
    df["years_required"] = [res.get("years_required") for res in results]
    df["summary_zh"] = [res.get("summary_zh") or res.get("error", "") for res in results]
    df["matched_skills"] = [", ".join(res.get("matched_skills", [])) for res in results]
    df["missing_skills"] = [", ".join(res.get("missing_skills", [])) for res in results]

    dropped = df.iloc[0:0]
    if cfg.get("filters", {}).get("require_sponsorship", True):
        no_visa = df["ai_sponsorship"] == "no_sponsorship"
        dropped = df[no_visa].assign(excluded_reason="Claude: no visa sponsorship",
                                     visa_evidence=df.loc[no_visa, "ai_sponsorship_evidence"])
        df = df[~no_visa]

    top = df[df["fit_score"].fillna(0) >= ccfg.get("tailor_min_score", 8)]
    df["letter_file"] = ""
    if not top.empty:
        letters_dir.mkdir(parents=True, exist_ok=True)
        print(f"Writing tailored material for {len(top)} top jobs...")
        for idx, r in top.iterrows():
            try:
                t = claude.tailor(posting_text(r))
            except (anthropic.APIError, RuntimeError, json.JSONDecodeError, StopIteration) as e:
                print(f"    {r['company']} - {r['title']}: {e}")
                continue
            name = re.sub(r"[^\w-]+", "_", f"{r['company']}_{r['title']}")[:80] + ".md"
            (letters_dir / name).write_text(
                f"# {r['title']} - {r['company']}\n\n{r['job_url']}\n\n## Resume bullets\n\n"
                + "\n".join(f"- {b}" for b in t["resume_bullets"])
                + f"\n\n## Cover letter\n\n{t['cover_letter']}\n",
                encoding="utf-8",
            )
            df.at[idx, "letter_file"] = f"letters/{name}"

    print(f"Claude usage: {claude.tokens_in:,} in / {claude.tokens_out:,} out tokens, about ${claude.cost():.2f}")
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
    sponsors = load_h1b_sponsors(cfg.get("h1b_sponsor_files") or [])

    raw = scrape(cfg)
    if raw.empty:
        print("No jobs found. Boards may be blocking you (try later, fewer searches, or proxies).")
        return
    shortlist, excluded = apply_filters(raw, cfg, seen, sponsors)
    print(f"Scraped {len(raw)}, new and passing filters: {len(shortlist)}, filtered out: {len(excluded)}")

    today = date.today().isoformat()
    run_dir = OUTPUT_DIR / today
    if use_claude and not shortlist.empty:
        shortlist, dropped = run_claude(shortlist, cfg, resume, run_dir / "letters")
        excluded = pd.concat([excluded, dropped])

    out = run_dir / f"jobs_{today}.xlsx"
    write_excel(out, shortlist, excluded)

    # Remember what we looked at, so tomorrow's run only shows new jobs. Without
    # Claude, the shortlist isn't remembered so a later full run still scores it.
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    looked_at = [shortlist, excluded] if use_claude else [excluded]
    new_seen = pd.concat(looked_at)[["key", "title", "company", "job_url"]]
    new_seen["first_seen"] = today
    new_seen.to_csv(SEEN_FILE, mode="a", header=not SEEN_FILE.exists(), index=False)

    print(f"Done: {out}")


if __name__ == "__main__":
    main()
