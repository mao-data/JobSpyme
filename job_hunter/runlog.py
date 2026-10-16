"""Retries and a record of what went wrong in a run.

Every network step (each search, each Claude call, each Google Sheet write) goes
through RunLog.attempt: up to 3 tries with a growing wait in between. A step that
still fails is recorded, and the run carries on with the rest. At the end the log
is printed, written to the GitHub Actions run page, and added to the sheet's
"Run log" tab.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class StepFailed(RuntimeError):
    pass


@contextmanager
def jobspy_errors():
    """Collect the errors JobSpy logs during a call. JobSpy reports a blocked
    request (403, 429) as a log line and an empty result, not an exception."""
    errors: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            errors.append(record.getMessage())

    handler = Collect(level=logging.ERROR)
    logger = logging.getLogger("jobspy")
    logger.addHandler(handler)
    try:
        yield errors
    finally:
        logger.removeHandler(handler)


class RunLog:
    def __init__(self, attempts: int = 3, waits: tuple[int, ...] = (15, 60), sleep=time.sleep):
        self.attempts = attempts
        self.waits = waits
        self.sleep = sleep
        self.started = datetime.now(timezone.utc)
        self.failures: list[tuple[str, str]] = []   # (step, last error)
        self.recovered: list[tuple[str, int]] = []  # (step, attempts it took)
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}
        self.accounting: list[str] = []      # filled by hunt.check_accounting
        self.accounting_ok: bool | None = None

    def attempt(self, step: str, fn, waits: tuple[int, ...] | None = None):
        """fn() up to `attempts` times. Returns its result, or None if every try failed."""
        waits = waits or self.waits
        for n in range(1, self.attempts + 1):
            try:
                result = fn()
            except Exception as e:  # each step decides what counts as failure by raising
                error = " ".join(str(e).split())[:300] or type(e).__name__
                if n < self.attempts:
                    wait = waits[min(n - 1, len(waits) - 1)]
                    print(f"    [retry {n}/{self.attempts - 1}] {step}: {error}; waiting {wait}s", flush=True)
                    self.sleep(wait)
                    continue
                print(f"    [FAILED after {n} tries] {step}: {error}", flush=True)
                self.failures.append((step, error))
                return None
            if n > 1:
                self.recovered.append((step, n))
                print(f"    [ok on try {n}] {step}", flush=True)
            return result
        return None

    def warn(self, message: str) -> None:
        print(f"    [warning] {message}", flush=True)
        self.warnings.append(message)

    def count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    # ---------- reporting ----------

    @property
    def status(self) -> str:
        if not self.failures:
            return "OK"
        return "失敗" if self.counts.get("jobs_added", 0) == 0 and self.counts.get("scraped", 0) == 0 else "部分失敗"

    @staticmethod
    def run_url() -> str:
        if os.environ.get("GITHUB_RUN_ID"):
            return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                    f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        return "(local run)"

    def summary_lines(self) -> list[str]:
        mins = (datetime.now(timezone.utc) - self.started).total_seconds() / 60
        lines = [f"Status: {self.status} ({mins:.0f} min)"]
        lines.append("Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        if self.recovered:
            lines.append(f"Recovered after retry ({len(self.recovered)}): "
                         + "; ".join(f"{s} (try {n})" for s, n in self.recovered))
        if self.failures:
            lines.append(f"Failed after {self.attempts} tries ({len(self.failures)}):")
            lines += [f"  - {s}: {e}" for s, e in self.failures]
        lines += [f"Warning: {w}" for w in self.warnings]
        if self.accounting:
            lines.append("Accounting: " + ("all jobs accounted for" if self.accounting_ok else "MISMATCH"))
        return lines

    def print_summary(self) -> None:
        print("\n===== Run summary =====")
        for line in self.summary_lines():
            print(line)

    def write_github_summary(self) -> None:
        """Shown on the Actions run page."""
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return
        md = [f"## Job Hunter: {self.status}", "", "| | |", "|---|---|"]
        md += [f"| {k} | {v} |" for k, v in sorted(self.counts.items())]
        if self.failures:
            md += ["", f"### Failed after {self.attempts} tries", ""]
            md += [f"- **{s}**: {e}" for s, e in self.failures]
        if self.recovered:
            md += ["", "### Recovered after retry", ""]
            md += [f"- {s} (try {n})" for s, n in self.recovered]
        if self.warnings:
            md += ["", "### Warnings", ""] + [f"- {w}" for w in self.warnings]
        if self.accounting:
            md += ["", "### Accounting (every scraped job accounted for?)", "", "```"] + self.accounting + ["```"]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")

    def sheet_row(self) -> dict:
        return {
            "run_time": self.started.strftime("%Y-%m-%d %H:%M UTC"),
            "status": self.status,
            "minutes": round((datetime.now(timezone.utc) - self.started).total_seconds() / 60),
            "scraped": self.counts.get("scraped", 0),
            "new_jobs": self.counts.get("jobs_added", 0),
            "scored": self.counts.get("scored", 0),
            "failed_steps": len(self.failures),
            "retried_ok": len(self.recovered),
            "details": "\n".join(f"{s}: {e}" for s, e in self.failures) or "",
            "per_site": ", ".join(f"{k[5:]}={v}" for k, v in sorted(self.counts.items()) if k.startswith("site_")),
            "run_url": self.run_url(),
            "check": "" if self.accounting_ok is None else ("OK" if self.accounting_ok else "不符"),
            "accounting": "\n".join(self.accounting),
        }
