"""Jira's rate limit (429): wait as asked, retry, and explain if it doesn't clear."""
import os
import threading
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import requests

from jira_exporter import jira_client
from jira_exporter.field_discovery import discover_fields
from jira_exporter.jira_client import CLOUD, Credentials, JiraClient, JiraError, _retry_after


def client(srv, deployment="datacenter"):
    return JiraClient(Credentials(f"http://127.0.0.1:{srv.server_address[1]}", "alice", "secret",
                                  deployment=deployment))


@pytest.fixture
def waits(monkeypatch):
    """Record waits instead of sleeping. waits.paced: waits spacing requests out."""
    class Seen(list):
        paced: list

    seen = Seen()
    seen.paced = []

    def wait(self, s, cancelled, pacing=False):
        (seen.paced if pacing else seen).append(round(s, 1))
        return True

    monkeypatch.setattr(JiraClient, "_wait", wait)
    return seen


def _resp(headers):
    r = requests.Response()
    r.status_code = 429
    r.headers.update(headers)
    return r


def test_retry_after_header_forms():
    assert _retry_after(_resp({"Retry-After": "7"}), 0, 60) == 7
    assert _retry_after(_resp({"Retry-After": "500"}), 0, 60) == 60  # capped
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert 25 <= _retry_after(_resp({"Retry-After": format_datetime(when, usegmt=True)}),
                              0, 60) <= 31
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert _retry_after(_resp({"Retry-After": format_datetime(past, usegmt=True)}), 0, 60) == 0
    # No (or unreadable) header: 2, 4, 8... seconds.
    assert [_retry_after(_resp({}), a, 60) for a in range(6)] == [2, 4, 8, 16, 32, 60]
    assert _retry_after(_resp({"Retry-After": "soon"}), 1, 60) == 4


@pytest.mark.parametrize("cloud", [False, True])
def test_requests_retry_after_429(jira_server, cloud_server, cloud, waits):
    srv = cloud_server if cloud else jira_server
    srv.rate_limit, srv.retry_after = 3, "5"
    c = client(srv, CLOUD if cloud else "datacenter")
    assert c.myself()
    assert waits == [5, 5, 5] and len(srv.limited) == 3


def test_backoff_without_retry_after(jira_server, waits):
    jira_server.rate_limit, jira_server.retry_after = 2, None
    assert client(jira_server).fields()
    assert waits == [2, 4]


def test_gives_up_after_five_minutes_with_details(jira_server, waits):
    jira_server.rate_limit, jira_server.retry_after = 100, "20"
    jira_server.limit_headers = {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0"}
    with pytest.raises(JiraError) as err:
        client(jira_server).fields()
    assert err.value.status == 429
    msg = str(err.value)
    assert waits == [20] * 15  # 300 s in all
    assert len(jira_server.limited) == 16
    assert "limiting how many requests" in msg and "Rate limit exceeded" in msg
    assert "waited 300 s in total and tried 16 times" in msg
    assert "Retry-After: 20" in msg and "X-RateLimit-Remaining: 0" in msg
    assert "(Request: GET /rest/api/2/field)" in msg


def test_short_retry_after_keeps_trying_but_not_forever(jira_server, waits):
    jira_server.rate_limit, jira_server.retry_after = 1000, "0"
    with pytest.raises(JiraError):
        client(jira_server).myself()
    assert len(jira_server.limited) == JiraClient.MAX_ATTEMPTS


def test_blocked_account_fails_at_once(jira_server, waits):
    jira_server.rate_limit = 100
    jira_server.limit_headers = {"X-RateLimit-Limit": "0", "X-RateLimit-FillRate": "0"}
    with pytest.raises(JiraError, match="allows no requests at all") as err:
        client(jira_server).myself()
    assert waits == [] and len(jira_server.limited) == 1
    assert "Jira administration" in str(err.value)


def test_waits_for_refill_and_paces_later_requests(jira_server, waits):
    # Data Center style: 1 request back every 5 s; Retry-After says only 1 s.
    jira_server.rate_limit, jira_server.retry_after = 1, "1"
    jira_server.limit_headers = {"X-RateLimit-FillRate": "1",
                                 "X-RateLimit-Interval-Seconds": "5"}
    c = client(jira_server)
    assert c.myself()
    assert waits == [5]
    c.fields()
    c.fields()
    # The clock doesn't move while waits are faked, so the slots pile up 5 s apart.
    assert waits.paced == [0.0, 5.0, 10.0]


def test_pacing_spaces_requests_in_real_time(jira_server, monkeypatch):
    monkeypatch.setattr(JiraClient, "MIN_GAP_AFTER_LIMIT", 0.3)
    jira_server.rate_limit, jira_server.retry_after = 1, "0"
    c = client(jira_server)
    c.myself()  # limited once, then served
    start = time.monotonic()
    for _ in range(3):
        c.myself()
    assert time.monotonic() - start >= 0.6


def test_discover_fields_survives_rate_limit(jira_server, waits):
    # 429s spread over the scan: the project, create screens and the issue sample.
    jira_server.rate_limit = 4
    result = discover_fields(client(jira_server), "ABC", sample_size=150)
    assert result.sampled == 150 and result.warnings == []
    assert len(waits) == 4


def test_export_search_survives_rate_limit(jira_server, waits):
    c = client(jira_server)
    pages = iter(c.search("project = ABC", ["summary"], page_size=100))
    first = [next(pages) for _ in range(100)]  # page 1 served
    jira_server.rate_limit = 2  # page 2 is limited twice, then served
    rest = list(pages)
    assert len(first) + len(rest) == 250 and len(waits) == 2


def test_cancel_during_wait_stops_quietly(jira_server, monkeypatch):
    monkeypatch.setattr(JiraClient, "MAX_WAIT", 30.0)
    jira_server.rate_limit, jira_server.retry_after = 100, "30"
    c = client(jira_server)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    issues = list(c.search("project = ABC", ["summary"], cancelled=stop.is_set))
    assert issues == [] and time.monotonic() - start < 3


def test_wait_notice_goes_to_the_waiting_thread(jira_server, monkeypatch):
    monkeypatch.setattr(jira_client.time, "sleep", lambda s: None)
    jira_server.rate_limit, jira_server.retry_after = 1, "3"
    c = client(jira_server)
    here, other = [], []
    c.notify_waits(here.append)
    t = threading.Thread(target=lambda: c.notify_waits(other.append))
    t.start()
    t.join()
    real_monotonic = time.monotonic
    clock = iter(range(0, 10000))
    monkeypatch.setattr(jira_client.time, "monotonic", lambda: next(clock))
    assert c.myself()
    monkeypatch.setattr(jira_client.time, "monotonic", real_monotonic)
    assert here == [3.0] and other == []


def test_scan_dialog_shows_the_wait(jira_server, tmp_path, monkeypatch):
    from test_fields import _dialog, _scan
    jira_server.rate_limit, jira_server.retry_after = 1, "1"
    qapp, dlg = _dialog(jira_server, tmp_path)
    shown = []
    say = dlg._say
    monkeypatch.setattr(dlg, "_say", lambda text, *a, **k: (shown.append(text), say(text, *a, **k)))
    assert _scan(qapp, dlg)
    assert any("slow down" in s for s in shown), shown
    assert dlg.result.sampled > 0
    dlg.reject()
