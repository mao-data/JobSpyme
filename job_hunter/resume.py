"""Tailored one-page resumes (and cover letters) for jobs you mark 想投.

Every fact comes from the experience bank (EXPERIENCE_BANK secret, or
experience_bank.yaml next to this file). Claude picks, orders and rewords those
facts for one job; a fact check then rejects any number or tool that is not in
the bank, and Claude tries again. The resume is rendered with RenderCV (PDF) and
also built as HTML for an editable Google Doc.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BANK_FILE = HERE / "experience_bank.yaml"


class FactCheckFailed(RuntimeError):
    pass


def load_bank() -> dict | None:
    raw = os.environ.get("EXPERIENCE_BANK")
    if not raw and BANK_FILE.exists():
        raw = BANK_FILE.read_text(encoding="utf-8")
    return yaml.safe_load(raw) if raw else None


# ---------- asking Claude ----------

SYSTEM = (
    "You tailor resumes. You only use facts from the candidate's experience bank: "
    "you may choose, reorder, merge and reword them to match a job, but you never add a "
    "number, tool, employer, title, responsibility or result the bank does not state. "
    "Write in plain, specific US resume English: strong verbs, no buzzwords, no first person."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences aimed at this job"},
        "experience": {
            "type": "array",
            "description": "Jobs from the bank, in the bank's order",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "experience id from the bank"},
                    "bullets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "fact_ids": {"type": "array", "items": {"type": "string"},
                                             "description": "bank fact ids this bullet is based on"},
                            },
                            "required": ["text", "fact_ids"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "bullets"],
                "additionalProperties": False,
            },
        },
        "skills": {
            "type": "array",
            "description": "Skill lines, most relevant first; items copied from the bank's skills",
            "items": {
                "type": "object",
                "properties": {"category": {"type": "string"},
                               "items": {"type": "array", "items": {"type": "string"}}},
                "required": ["category", "items"],
                "additionalProperties": False,
            },
        },
        "jd_keywords": {
            "type": "array", "items": {"type": "string"},
            "description": "10-20 hard skills, tools and domain terms the posting asks for, written exactly as in the posting",
        },
        "changes_zh": {"type": "string",
                       "description": "In Traditional Chinese, 2-4 short lines: what was emphasized for this job and why"},
        "cover_letter": {"type": "string",
                         "description": "Plain-text cover letter, 3 short paragraphs, 220-320 words, no greeting line or signature"},
    },
    "required": ["summary", "experience", "skills", "jd_keywords", "changes_zh", "cover_letter"],
    "additionalProperties": False,
}


def bank_for_prompt(bank: dict) -> str:
    keep = {k: bank[k] for k in ("target_roles", "rules", "experience", "education", "skills", "certifications") if k in bank}
    return yaml.safe_dump(keep, allow_unicode=True, sort_keys=False, width=200)


def build_prompt(bank: dict, job: dict, problems: list[str] | None = None) -> str:
    must_keep = [e["id"] for e in bank["experience"] if not e.get("optional")]
    parts = [
        "Tailor the candidate's resume and write a cover letter for this job.",
        "",
        "Rules:",
        "- Use only facts from the experience bank below. Every bullet lists the fact ids it is based on, "
        "and may only contain numbers that appear in those facts.",
        "- Follow every line under `rules:` in the bank.",
        f"- Include these jobs: {', '.join(must_keep)}. Optional jobs only if relevant and there is room.",
        "- It must fit on ONE page: 11-14 bullets in total. Give the most relevant job 4-6 bullets "
        "and less relevant jobs 1-3. Each bullet at most 2 lines (about 200 characters).",
        "- Lead with what this posting cares about most. Use the posting's own wording for a skill "
        "only when the bank shows the candidate has that skill.",
        "- Skills: only items that appear in the bank's skills list; you may drop or reorder items and lines.",
        "- Cover letter: specific to this company and role, uses the same facts, never mentions visas "
        "or work authorization.",
        "",
        "Experience bank:",
        bank_for_prompt(bank),
        "",
        f"Job: {job.get('title', '')} at {job.get('company', '')} ({job.get('location', '')})",
        "Posting:",
        (job.get("description") or "(no description available; tailor to the title)")[:12000],
    ]
    if problems:
        parts += ["", "Your previous draft broke these rules. Fix all of them:"] + [f"- {p}" for p in problems]
    return "\n".join(parts)


# ---------- fact check ----------

# Tools and platforms a draft might add. Any of these in the output must also be in the bank.
TOOLS = [
    "aws", "azure", "snowflake", "databricks", "spark", "pyspark", "hadoop", "hive", "airflow", "dbt",
    "kafka", "sap", "oracle", "salesforce", "jira", "confluence", "vba", "power query", "dax", "ssis",
    "ssrs", "alteryx", "sas", "spss", "stata", "matlab", "qlik", "domo", "looker studio", "hubspot",
    "zendesk", "servicenow", "kubernetes", "docker", "terraform", "java", "javascript", "typescript",
    "c\\+\\+", "c#", "scala", "ruby", "react", "node\\.js", "django", "flask", "tensorflow", "pytorch",
    "keras", "redshift", "synapse", "microsoft fabric", "sharepoint", "power automate", "power apps",
    "uipath", "workday", "coupa", "ariba", "blue yonder", "kinaxis", "manhattan", "sql server", "t-sql",
    "mongodb", "nosql", "graphql", "excel vba", "google analytics", "ga4", "mixpanel", "amplitude",
    "segment", "fivetran", "informatica", "talend", "ssms", "postgres", "mysql", "bigquery", "tableau",
    "power bi", "looker", "netsuite", "python", "r", "sql", "gcp", "git", "linux", "pandas", "numpy",
    "scikit-learn", "sentry", "postman", "pylon", "dataflow", "pdfplumber", "seaborn", "matplotlib",
    "edi", "wms", "tms", "erp", "six sigma", "lean", "pmp", "cscp", "apics",
]
TOOL_RE = re.compile(r"(?<![\w-])(" + "|".join(sorted(TOOLS, key=len, reverse=True)) + r")(?![\w-])", re.I)
NUM_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?\s*[kKmM]?(?![a-zA-Z])")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(text or ""):
        m = m.replace(",", "").replace(" ", "")
        mult = {"k": 1_000, "m": 1_000_000}.get(m[-1].lower(), 1)
        n = float(m.rstrip("kKmM")) * mult
        out.add(f"{n:g}")
    return out


def _tools(text: str) -> set[str]:
    return {m.lower() for m in TOOL_RE.findall(text or "")}


def bank_text(bank: dict) -> str:
    return yaml.safe_dump(bank, allow_unicode=True)


def fact_check(draft: dict, bank: dict, company: str = "") -> list[str]:
    """Problems that make a draft unusable, as instructions Claude can act on."""
    problems: list[str] = []
    facts = {f["id"]: f for e in bank["experience"] for f in e["facts"]}
    exp_ids = {e["id"] for e in bank["experience"]}
    all_text = bank_text(bank)
    all_numbers, all_tools = _numbers(all_text), _tools(all_text)
    allowed_tools = all_tools | _tools(company)

    seen = set()
    for exp in draft.get("experience", []):
        if exp["id"] not in exp_ids:
            problems.append(f"Unknown experience id {exp['id']!r}; use only ids from the bank.")
            continue
        seen.add(exp["id"])
        for b in exp["bullets"]:
            cited = [facts[i] for i in b["fact_ids"] if i in facts]
            bad_ids = [i for i in b["fact_ids"] if i not in facts]
            if bad_ids:
                problems.append(f"Bullet {b['text'][:60]!r} cites unknown fact ids {bad_ids}.")
            if not cited:
                problems.append(f"Bullet {b['text'][:60]!r} cites no bank fact.")
                continue
            source = " ".join(f["text"] + " " + " ".join(f.get("tools", [])) for f in cited)
            extra = _numbers(b["text"]) - _numbers(source)
            if extra:
                problems.append(f"Bullet {b['text'][:60]!r} has numbers {sorted(extra)} that are not in "
                                f"the facts it cites ({', '.join(f['id'] for f in cited)}).")
            new_tools = _tools(b["text"]) - allowed_tools
            if new_tools:
                problems.append(f"Bullet {b['text'][:60]!r} mentions {sorted(new_tools)}, which the bank does not list.")
    for e in bank["experience"]:
        if not e.get("optional") and e["id"] not in seen:
            problems.append(f"Job {e['id']!r} is missing; every non-optional job must stay on the resume.")

    for name, text in (("summary", draft.get("summary", "")), ("cover letter", draft.get("cover_letter", ""))):
        extra = _numbers(text) - all_numbers
        if extra:
            problems.append(f"The {name} has numbers {sorted(extra)} that are not in the bank.")
        new_tools = _tools(text) - allowed_tools
        if new_tools:
            problems.append(f"The {name} mentions {sorted(new_tools)}, which the bank does not list.")
        if re.search(r"\b(visa|h-?1b|sponsor\w*|work authori[sz]ation|green card|opt)\b", text, re.I):
            problems.append(f"The {name} mentions visa or work authorization; remove it.")
    return problems


def clean_skills(draft: dict, bank: dict) -> list[tuple[str, list[str]]]:
    """Skill lines with anything not in the bank removed (no retry needed for that)."""
    known = {}
    for items in bank.get("skills", {}).values():
        for s in items:
            known[s.lower()] = s
    lines = []
    for line in draft.get("skills", []):
        items = [known[i.lower()] for i in line["items"] if i.lower() in known]
        if items:
            lines.append((line["category"], list(dict.fromkeys(items))))
    return lines or [(k, v) for k, v in bank.get("skills", {}).items()]


# ---------- keyword coverage ----------

def coverage(keywords: list[str], description: str, before: str, after: str) -> tuple[str, list[str]]:
    """'6/12 → 10/12' for the posting's keywords found in the old and new resume, plus what's still missing."""
    desc = (description or "").lower()
    # Only keywords really in the posting; without a posting there is nothing to measure.
    kws = [k for k in dict.fromkeys(k.strip() for k in keywords) if k and k.lower() in desc]
    if not kws:
        return "", []
    has = lambda text, k: re.search(r"(?<!\w)" + re.escape(k.lower()) + r"(?!\w)", text.lower()) is not None
    n_before = sum(has(before, k) for k in kws)
    missing = [k for k in kws if not has(after, k)]
    return f"{n_before}/{len(kws)} → {len(kws) - len(missing)}/{len(kws)}", missing


# ---------- the resume itself ----------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _ym(text: str) -> str:
    """'Jul 2026' -> '2026-07'; 'Present' -> 'present'."""
    t = str(text).strip()
    if t.lower() == "present":
        return "present"
    mon, year = t.split()
    return f"{year}-{MONTHS[mon[:3].lower()]:02d}"


def assemble(bank: dict, draft: dict, drop: int = 0) -> dict:
    """The resume content, with `drop` lines removed to fit one page: first the
    certifications, then optional jobs, then the last bullet of the longest job."""
    by_id = {e["id"]: e for e in bank["experience"]}
    jobs = [(by_id[e["id"]], [b["text"] for b in e["bullets"]]) for e in draft["experience"] if e["id"] in by_id]
    jobs.sort(key=lambda j: bank["experience"].index(j[0]))
    certs = list(bank.get("certifications", []))
    for _ in range(drop):
        if certs:
            certs = []
            continue
        optional = [j for j in jobs if j[0].get("optional")]
        if optional:
            jobs.remove(optional[-1])
            continue
        longest = max(jobs, key=lambda j: len(j[1]))
        if len(longest[1]) <= 1:
            break
        longest[1].pop()
    return {"summary": draft["summary"], "jobs": jobs, "skills": draft["skills_clean"], "certifications": certs}


def plain_text(bank: dict, content: dict) -> str:
    lines = [bank["contact"]["name"], content["summary"]]
    for exp, bullets in content["jobs"]:
        lines += [f"{exp['company']} {exp['title']}"] + bullets
    lines += [f"{c}: {', '.join(i)}" for c, i in content["skills"]]
    lines += [ed["degree"] + " " + ed["school"] for ed in bank.get("education", [])]
    return "\n".join(lines + content["certifications"])


def _md(text: str) -> str:
    """Escape characters RenderCV's markdown would treat as formatting."""
    return re.sub(r"([*_`\[\]#])", r"\\\1", str(text))


def rendercv_yaml(bank: dict, content: dict) -> dict:
    c = bank["contact"]
    phone = re.sub(r"\D", "", c.get("phone", ""))
    sections = {"Summary": [_md(content["summary"])], "Experience": [], "Education": []}
    for exp, bullets in content["jobs"]:
        sections["Experience"].append({
            "company": exp["company"], "position": exp["title"],
            "start_date": _ym(exp["start"]), "end_date": _ym(exp["end"]),
            "highlights": [_md(b) for b in bullets],
        })
    for ed in bank.get("education", []):
        highlights = ([f"GPA: {ed['gpa']}"] if ed.get("gpa") else []) + list(ed.get("extras", []))
        sections["Education"].append({
            "company": ed["school"], "position": ed["degree"], "location": ed.get("location"),
            "start_date": _ym(ed["start"]), "end_date": _ym(ed["end"]),
            "highlights": [_md(h) for h in highlights],
        })
    sections["Skills"] = [{"label": cat, "details": _md(", ".join(items))} for cat, items in content["skills"]]
    if content["certifications"]:
        sections["Certifications"] = [_md(", ".join(content["certifications"]))]
    cv = {"name": c["name"], "email": c.get("email"), "sections": sections}
    if phone:
        cv["phone"] = "+1" + phone[-10:] if len(phone) == 10 else "+" + phone
    if c.get("linkedin"):
        cv["social_networks"] = [{"network": "LinkedIn", "username": c["linkedin"].rstrip("/").split("/")[-1]}]
    return {
        "cv": cv,
        "design": {
            "theme": "classic",
            "page": {"size": "us-letter", "top_margin": "0.45in", "bottom_margin": "0.45in",
                     "left_margin": "0.55in", "right_margin": "0.55in",
                     "show_footer": False, "show_top_note": False},
            "typography": {"font_size": {"body": "10pt", "name": "24pt"}, "line_spacing": "0.5em",
                           "alignment": "left"},
            "sections": {"show_time_spans_in": []},
            "header": {"space_below_name": "0.3cm", "space_below_connections": "0.3cm"},
        },
        "settings": {"render_command": {"dont_generate_markdown": True, "dont_generate_html": True,
                                        "dont_generate_png": True}},
    }


def render_pdf(bank: dict, content: dict, workdir: Path) -> tuple[bytes, int]:
    """(PDF bytes, page count)."""
    from pypdf import PdfReader

    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "resume.yaml"
    src.write_text(yaml.safe_dump(rendercv_yaml(bank, content), allow_unicode=True, sort_keys=False), encoding="utf-8")
    out = workdir / "out"
    proc = subprocess.run([sys.executable, "-m", "rendercv", "render", str(src), "--output-folder", str(out)],
                          capture_output=True, text=True, timeout=300)
    pdfs = list(out.glob("*.pdf"))
    if proc.returncode != 0 or not pdfs:
        raise RuntimeError(f"RenderCV failed: {(proc.stderr or proc.stdout)[-600:]}")
    data = pdfs[0].read_bytes()
    return data, len(PdfReader(pdfs[0]).pages)


def one_page_pdf(bank: dict, draft: dict) -> tuple[bytes, dict]:
    """Render, trimming lines until the resume fits on one page."""
    with tempfile.TemporaryDirectory() as tmp:
        for drop in range(0, 12):
            content = assemble(bank, draft, drop)
            data, pages = render_pdf(bank, content, Path(tmp) / str(drop))
            if pages <= 1:
                return data, content
    return data, content  # still two pages after trimming: keep the last try


def resume_html(bank: dict, content: dict) -> str:
    """Simple HTML that Google Docs converts into an editable document."""
    e = html.escape
    c = bank["contact"]
    contact = " | ".join(e(x) for x in (c.get("phone"), c.get("email"), c.get("linkedin")) if x)
    out = [
        "<html><body style='font-family:Arial;font-size:10pt'>",
        f"<h1 style='text-align:center;font-size:20pt;margin:0'>{e(c['name'])}</h1>",
        f"<p style='text-align:center;margin:0'>{contact}</p>",
        "<h2 style='font-size:12pt'>Summary</h2>", f"<p>{e(content['summary'])}</p>",
        "<h2 style='font-size:12pt'>Experience</h2>",
    ]
    for exp, bullets in content["jobs"]:
        out.append(f"<p><b>{e(exp['company'])}</b> | {e(exp['title'])} &nbsp; <i>{e(exp['start'])} – {e(exp['end'])}</i></p>")
        out.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in bullets) + "</ul>")
    out.append("<h2 style='font-size:12pt'>Education</h2>")
    for ed in bank.get("education", []):
        extras = ([f"GPA: {ed['gpa']}"] if ed.get("gpa") else []) + list(ed.get("extras", []))
        out.append(f"<p><b>{e(ed['school'])}</b> | {e(ed['degree'])} &nbsp; <i>{e(ed['start'])} – {e(ed['end'])}</i></p>")
        if extras:
            out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in extras) + "</ul>")
    out.append("<h2 style='font-size:12pt'>Skills</h2>")
    out += [f"<p><b>{e(cat)}:</b> {e(', '.join(items))}</p>" for cat, items in content["skills"]]
    if content["certifications"]:
        out += ["<h2 style='font-size:12pt'>Certifications</h2>", f"<p>{e(', '.join(content['certifications']))}</p>"]
    return "".join(out) + "</body></html>"


def letter_html(bank: dict, job: dict, letter: str) -> str:
    e = html.escape
    c = bank["contact"]
    paras = [p.strip() for p in letter.split("\n") if p.strip()]
    return ("<html><body style='font-family:Arial;font-size:11pt'>"
            f"<p>{e(c['name'])}<br>{e(c.get('email', ''))} | {e(c.get('phone', ''))}</p>"
            f"<p>Dear {e(job.get('company') or 'Hiring')} Hiring Team,</p>"
            + "".join(f"<p>{e(p)}</p>" for p in paras)
            + f"<p>Sincerely,<br>{e(c['name'])}</p></body></html>")


def file_stem(bank: dict, job: dict, today: str) -> str:
    clean = lambda s: "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", str(s or "")))[:30]
    return f"{clean(bank['contact']['name'])}_Resume_{clean(job.get('company'))}_{clean(job.get('title'))}_{today}"


# ---------- one run: every job marked 想投 ----------

def fetch_posting(url: str) -> str:
    """Posting text from the job page, for jobs saved before descriptions were kept."""
    if not url:
        return ""
    try:
        import requests
        from bs4 import BeautifulSoup

        r = requests.get(url, timeout=30, headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))
        return text if len(text) > 800 else ""
    except Exception:
        return ""


def _link(url: str, label: str) -> str:
    return f'=HYPERLINK("{url}", "{label}")'


def make_resumes(book, cfg: dict, log, old_resume: str, today: str) -> None:
    """Tailored resume + cover letter for each Jobs row whose status is 想投 and has
    no resume yet. Links and a summary of the changes go into that row."""
    import drive
    import sheets
    from llm import LLMError, make_llm

    rcfg = cfg.get("resume", {})
    if not rcfg.get("enabled", True):
        return
    max_tries = rcfg.get("max_tries", 3)
    jobs = log.attempt("Google Sheet: read 想投 jobs", lambda: sheets.wanted_jobs(book, max_tries))
    if not jobs:
        print("Resumes: no 想投 jobs waiting for a resume")
        return
    print(f"Resumes: {len(jobs)} 想投 job(s) need a tailored resume", flush=True)
    bank = load_bank()
    if not bank:
        log.warn("Resumes skipped: no experience bank (EXPERIENCE_BANK secret or experience_bank.yaml)")
        return
    if not os.environ.get("GOOGLE_OAUTH_TOKEN_JSON"):
        log.warn("Resumes skipped: GOOGLE_OAUTH_TOKEN_JSON secret is not set")
        return
    jobs = jobs[: rcfg.get("max_per_run", 10)]
    descs = log.attempt("Google Sheet: read job descriptions",
                        lambda: sheets.descriptions(book, {j["key"] for j in jobs})) or {}
    service = log.attempt("Google Drive: connect", drive.connect)
    folder_id = service and log.attempt(
        "Google Drive: resume folder",
        lambda: drive.folder(service, [rcfg.get("drive_folder", "Job Hunter Resumes"), today[:7]]))
    if not folder_id:
        return
    claude_cfg = {**cfg.get("claude", {}), "cli_model": rcfg.get("cli_model", cfg.get("claude", {}).get("cli_model"))}
    llm = make_llm(claude_cfg, SYSTEM)

    for job in jobs:
        name = f"{job.get('title', '')} @ {job.get('company', '')}"
        print(f"  resume for {name}", flush=True)
        description = descs.get(job["key"]) or fetch_posting(job.get("job_url_direct") or job.get("job_url"))
        job["description"] = description
        try:
            draft, last_error = None, ""
            problems: list[str] = []
            for _ in range(rcfg.get("fact_check_tries", 3)):
                try:
                    draft = llm.ask(build_prompt(bank, job, problems), SCHEMA, effort="medium")
                except LLMError as e:
                    problems, draft, last_error = [], None, str(e)
                    print(f"    Claude error: {e}", flush=True)
                    continue
                problems = fact_check(draft, bank, job.get("company", ""))
                if not problems:
                    break
                print(f"    fact check: {len(problems)} problem(s); asking for a corrected draft", flush=True)
            if draft is None:
                raise RuntimeError(f"Claude did not answer: {last_error}")
            if problems:
                raise FactCheckFailed("fact check failed: " + "; ".join(problems)[:300])
            draft["skills_clean"] = clean_skills(draft, bank)
            pdf, content = one_page_pdf(bank, draft)
            stem = file_stem(bank, job, today)
            links = {}
            for col, fname, data, mime, as_doc in (
                ("resume_pdf", stem + ".pdf", pdf, "application/pdf", False),
                ("resume_doc", stem, resume_html(bank, content).encode(), "text/html", True),
                ("cover_letter_doc", stem.replace("_Resume_", "_CoverLetter_"),
                 letter_html(bank, job, draft["cover_letter"]).encode(), "text/html", True),
            ):
                url = log.attempt(f"Google Drive: upload {fname}",
                                  lambda: drive.upload(service, folder_id, fname, data, mime, as_doc))
                if url is None:
                    raise RuntimeError(f"upload of {fname} failed")
                links[col] = url
            cov, missing = coverage(draft.get("jd_keywords", []), description, old_resume, plain_text(bank, content))
            changes = draft.get("changes_zh", "").strip()
            if not description:
                changes = "（找不到完整職缺描述，依職稱撰寫）\n" + changes
            values = {
                "resume_pdf": _link(links["resume_pdf"], "PDF"),
                "resume_doc": _link(links["resume_doc"], "Doc"),
                "cover_letter_doc": _link(links["cover_letter_doc"], "Cover letter"),
                "resume_changes": changes,
                "keyword_coverage": (cov + ("；仍缺：" + ", ".join(missing) if missing else "")) if cov else "",
            }
            log.count("resumes_made")
        except Exception as e:
            reason = " ".join(str(e).split())[:300] or type(e).__name__
            n = job["failures"] + 1
            print(f"    resume failed ({n}/{max_tries}): {reason}", flush=True)
            log.failures.append((f"Resume: {name}", reason))
            log.count("resumes_failed")
            values = {"resume_changes": f"{sheets.FAILED_PREFIX}（第 {n} 次）：{reason}"
                                        + ("。清空這格可以再試" if n >= max_tries else "。下次執行會再試")}
        if log.attempt(f"Google Sheet: write resume links for {name}",
                       lambda: sheets.write_resume_result(book, job["key"], values)) is False:
            log.warn(f"Resume for {name}: the row is gone from Jobs, links not written")
    print(f"    {llm.usage_note()}", flush=True)
