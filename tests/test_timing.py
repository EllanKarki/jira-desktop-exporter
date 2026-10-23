"""Export timing: a line per page, one-off steps, a summary and a log file."""
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from jira_exporter.app import ExportWorker
from jira_exporter.config import load_config
from jira_exporter.jira_client import CLOUD, Credentials, JiraClient
from jira_exporter.timing import ExportTimer, _clock

ROOT = Path(__file__).resolve().parents[1]


def _run(srv, tmp_path, deployment="datacenter", timeout=120.0, page_size=100):
    c = JiraClient(Credentials(f"http://127.0.0.1:{srv.server_address[1]}", "alice", "secret",
                               deployment=deployment), timeout=timeout)
    cfg = load_config(ROOT / "config" / "fields.json")
    cfg.page_size = page_size
    w = ExportWorker(c, cfg, "project = ABC", tmp_path / "out.xlsx")
    seen = {"log": [], "done": [], "failed": []}
    w.log.connect(seen["log"].append)
    w.finished.connect(lambda n, p: seen["done"].append(n))
    w.failed.connect(seen["failed"].append)
    w.run()  # on this thread: signals are delivered directly
    return seen


def test_export_logs_each_page_and_a_summary(jira_server, tmp_path):
    seen = _run(jira_server, tmp_path)
    assert seen["done"] == [250] and seen["failed"] == []
    log = seen["log"]
    pages = [line for line in log if line.startswith("[time] Page ")]
    assert len(pages) == 3
    assert re.match(r"\[time\] Page 1: 100 issues, [\d,]+ KB in [\d.]+ s \(Jira [\d.]+ s, "
                    r"JSON [\d.]+ s, rows [\d.]+ s, sheet [\d.]+ s\); 100/250 issues after "
                    r"\d+m \d\ds$", pages[0]), pages[0]
    assert pages[2].startswith("[time] Page 3: 50 issues")
    text = "\n".join(log)
    for step in ("Read the field list from Jira", "Matched the configured fields",
                 "Formatted the sheet", "Saved out.xlsx", "Summary:",
                 "Waiting for Jira to answer:", "Turning issues into rows:",
                 "Adding rows to the sheet:", "Saving the .xlsx file:", "Everything else:",
                 "Pages: 3, average fetch", "Timeouts: 0, pages made smaller: 0"):
        assert step in text, step
    shares = [int(m) for m in re.findall(r"\((\d+)%\)", text)]
    assert 95 <= sum(shares) <= 105  # the categories add up to the whole


def test_timing_log_file_is_saved_next_to_the_export(jira_server, tmp_path):
    seen = _run(jira_server, tmp_path)
    path = tmp_path / "out-timing.log"
    assert f"Timing log saved to {path}" in seen["log"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith("Jira export timing log, ")
    assert "JQL: project = ABC" in text and "page size 100" in text
    assert re.search(r"^\d\d:\d\d:\d\d  \[time\] Page 3: 50 issues", text, re.M)
    assert "[time] Summary:" in text


def test_failed_export_still_saves_timing(jira_server, tmp_path, monkeypatch):
    monkeypatch.setattr(JiraClient, "TIMEOUT_RETRY_WAITS", (0.0, 0.0))
    jira_server.slow_above = 0  # every page is too slow
    jira_server.slow_delay = 0.5
    seen = _run(jira_server, tmp_path, timeout=0.2, page_size=20)
    assert seen["failed"] and "didn't answer" in seen["failed"][0]
    text = (tmp_path / "out-timing.log").read_text(encoding="utf-8")
    # 20 -> 10, then 10 tried three times: four timeouts, one smaller page.
    assert "Timeouts: 4, pages made smaller: 1" in text
    assert "Error: Jira at" in text


def test_rate_limit_waits_are_counted(jira_server, tmp_path, monkeypatch):
    monkeypatch.setattr(JiraClient, "_sleep", staticmethod(lambda s, c: True))
    jira_server.rate_limit, jira_server.retry_after = 2, "3"
    seen = _run(jira_server, tmp_path)
    assert seen["done"] == [250]
    assert any("rate-limit waits: 2" in line for line in seen["log"])


def test_cloud_logs_the_count_step(cloud_server, tmp_path):
    seen = _run(cloud_server, tmp_path, deployment=CLOUD)
    assert seen["done"] == [250]
    assert any("Counted matching issues" in line for line in seen["log"])


def test_clock_format():
    assert _clock(5) == "0m 05s"
    assert _clock(3725) == "1h 02m 05s"


def test_timer_page_without_total():
    lines = []
    t = ExportTimer(lines.append)
    t.page(10, 10, 0)
    assert "; 10 issues after" in lines[0]
