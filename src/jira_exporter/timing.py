"""Where an export spends its time: per-page lines, one-off steps and a summary."""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable

# Categories, in the order the summary lists them.
JIRA = "Waiting for Jira to answer"
WAITS = "Pausing (rate limit, retries)"
JSON = "Reading Jira's JSON"
ROWS = "Turning issues into rows"
EXCEL = "Adding rows to the sheet"
FINISH = "Formatting the sheet (widths, dates, filter)"
SAVE = "Saving the .xlsx file"
ORDER = (JIRA, WAITS, JSON, ROWS, EXCEL, FINISH, SAVE)


class ExportTimer:
    """Collects timings from the Jira client and the Excel writer.

    `add` is called with each measured piece; `step` and `page` turn what was
    measured since the previous call into one log line.
    """

    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self.started = self._mark = time.perf_counter()
        self.totals: dict[str, float] = defaultdict(float)
        self._since: dict[str, float] = defaultdict(float)
        self.bytes = self._bytes_since = 0
        self.timeouts = self.shrinks = self.limit_waits = 0
        self.pages: list[tuple[int, float]] = []  # (page number, fetch seconds)

    def log(self, text: str):
        self._log(text)

    def add(self, category: str, seconds: float):
        self.totals[category] += seconds
        self._since[category] += seconds

    def add_bytes(self, n: int):
        self.bytes += n
        self._bytes_since += n

    @contextmanager
    def timed(self, category: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self.add(category, time.perf_counter() - t)

    def _take(self) -> tuple[float, dict[str, float], int]:
        now = time.perf_counter()
        wall, since, size = now - self._mark, dict(self._since), self._bytes_since
        self._mark, self._bytes_since = now, 0
        self._since.clear()
        return wall, since, size

    def step(self, label: str):
        """Log the time since the previous step or page as one finished step."""
        wall, _, _ = self._take()
        self.log(f"[time] {label}: {_secs(wall)}")

    def page(self, issues: int, done: int, total: int):
        """Log the page that just finished: fetched, read, converted and added."""
        wall, s, size = self._take()
        number = len(self.pages) + 1
        fetch = s.get(JIRA, 0.0) + s.get(WAITS, 0.0) + s.get(JSON, 0.0)
        self.pages.append((number, fetch))
        parts = [f"Jira {_secs(s.get(JIRA, 0.0))}", f"JSON {_secs(s.get(JSON, 0.0))}",
                 f"rows {_secs(s.get(ROWS, 0.0))}", f"sheet {_secs(s.get(EXCEL, 0.0))}"]
        if s.get(WAITS):
            parts.append(f"paused {_secs(s[WAITS])}")
        of = f"/{total:,}" if total else ""
        self.log(f"[time] Page {number}: {issues} issues, {size / 1024:,.0f} KB in "
                 f"{_secs(wall)} ({', '.join(parts)}); {done:,}{of} issues after "
                 f"{_clock(time.perf_counter() - self.started)}")

    def summary(self) -> list[str]:
        whole = time.perf_counter() - self.started
        lines = [f"[time] Summary: {_clock(whole)} in total, "
                 f"{self.bytes / 1024 / 1024:,.1f} MB received from Jira"]
        counted = 0.0
        for cat in ORDER:
            seconds = self.totals.get(cat, 0.0)
            counted += seconds
            lines.append(f"[time]   {cat}: {_secs(seconds)} ({_pct(seconds, whole)})")
        other = max(0.0, whole - counted)
        lines.append(f"[time]   Everything else: {_secs(other)} ({_pct(other, whole)})")
        if self.pages:
            avg = sum(f for _, f in self.pages) / len(self.pages)
            slow_n, slow = max(self.pages, key=lambda p: p[1])
            lines.append(f"[time] Pages: {len(self.pages)}, average fetch {_secs(avg)}, "
                         f"slowest page {slow_n} ({_secs(slow)})")
        lines.append(f"[time] Timeouts: {self.timeouts}, pages made smaller: {self.shrinks}, "
                     f"rate-limit waits: {self.limit_waits}")
        for line in lines:
            self.log(line)
        return lines


def _secs(s: float) -> str:
    return f"{s:,.2f} s" if s < 100 else f"{s:,.0f} s"


def _clock(s: float) -> str:
    s = int(round(s))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole > 0 else "0%"
