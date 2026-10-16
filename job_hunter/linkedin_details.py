"""Fetch LinkedIn job descriptions slowly, only for jobs worth reading.

JobSpy fetches every LinkedIn job page back to back, which gets a 429 from LinkedIn
after a few dozen requests. Here the search runs without descriptions, the cheap
filters run first, and only the jobs left get their page fetched, with a pause
between requests and a back-off when LinkedIn starts refusing.
"""

from __future__ import annotations

import random
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def job_id_from_url(url: str) -> str | None:
    tail = str(url or "").rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else None


def fetch_description(session: requests.Session, job_id: str) -> tuple[int, str]:
    """(HTTP status, description text). Text is empty unless status is 200."""
    try:
        response = session.get(f"https://www.linkedin.com/jobs/view/{job_id}", timeout=30)
    except requests.RequestException:
        return 0, ""
    if response.status_code != 200:
        return response.status_code, ""
    markup = BeautifulSoup(response.text, "html.parser").find("div", class_="show-more-less-html__markup")
    return 200, markup.get_text("\n", strip=True) if markup else ""


def fill_descriptions(urls: list[str], opts: dict, proxies: list[str] | None = None,
                      fetch=fetch_description, sleep=time.sleep, log=None) -> dict[str, str]:
    """Description per job URL, for as many as LinkedIn lets us read. Each page gets
    up to 3 tries; `log` (a RunLog) records the pages that still failed."""
    session = requests.Session()
    session.headers.update(HEADERS)
    proxy_cycle = proxies or [None]
    delay_min, delay_max = opts.get("detail_delay", [5, 9])
    backoff = opts.get("backoff_seconds", 70)
    tries = opts.get("tries", 3)
    limit = opts.get("max_descriptions", 80)
    out: dict[str, str] = {}
    failed: list[str] = []
    blocked_in_a_row = 0
    todo = [(u, job_id_from_url(u)) for u in urls[:limit] if job_id_from_url(u)]
    print(f"Fetching {len(todo)} LinkedIn descriptions slowly...", flush=True)
    for n, (url, job_id) in enumerate(todo):
        if n:
            sleep(random.uniform(delay_min, delay_max))
        proxy = proxy_cycle[n % len(proxy_cycle)]
        if proxy and proxy != "localhost":
            p = proxy if "://" in proxy else f"http://{proxy}"
            session.proxies = {"http": p, "https": p}
        else:
            session.proxies = {}
        for attempt in range(1, tries + 1):
            status, text = fetch(session, job_id)
            if status == 200 or attempt == tries:
                break
            # 429 means slow down a lot; anything else (network error, 5xx) a little.
            wait = backoff if status == 429 else 10
            print(f"    [retry {attempt}/{tries - 1}] LinkedIn job {job_id}: HTTP {status or 'error'}; "
                  f"waiting {wait}s", flush=True)
            sleep(wait)
        if status == 200:
            blocked_in_a_row = 0
            if text:
                out[url] = text
            continue
        failed.append(f"{job_id} (HTTP {status or 'error'})")
        blocked_in_a_row = blocked_in_a_row + 1 if status == 429 else 0
        if blocked_in_a_row >= 3:
            print("    LinkedIn keeps refusing; skipping the remaining descriptions this run", flush=True)
            if log is not None:
                log.warn(f"LinkedIn job pages: stopped after 3 jobs in a row were refused {tries} times; "
                         f"{len(todo) - n - 1} left without a description")
            break
    print(f"    got {len(out)} of {len(todo)}", flush=True)
    if failed and log is not None:
        log.failures.append((f"LinkedIn job pages ({len(failed)} of {len(todo)})",
                             f"no description after {tries} tries: " + ", ".join(failed[:10])
                             + (" ..." if len(failed) > 10 else "")))
    return out
