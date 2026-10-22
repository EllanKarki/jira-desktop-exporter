"""Minimal Jira REST client for Data Center (API v2) and Cloud (API v3)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator
from urllib.parse import quote, urlparse

import requests

DATA_CENTER = "datacenter"
CLOUD = "cloud"


class JiraError(Exception):
    """Raised when Jira returns an error or cannot be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status, when Jira answered


class Cancelled(JiraError):
    """The caller cancelled while the client was waiting to retry."""


def guess_deployment(url: str) -> str:
    """Atlassian-hosted sites live on *.atlassian.net; anything else is assumed self-hosted."""
    try:
        host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:  # e.g. a stray "]" makes urlparse reject the host
        return DATA_CENTER
    return CLOUD if host.endswith(".atlassian.net") else DATA_CENTER


@dataclass
class Credentials:
    base_url: str
    # Data Center: username. Cloud: the account's email address.
    username: str
    # Data Center: password, or a Personal Access Token when use_token is set.
    # Cloud: an API token (Cloud does not accept account passwords for the API).
    secret: str
    # Data Center only: send `secret` as a Bearer Personal Access Token (DC 8.14+).
    use_token: bool = False
    verify_ssl: bool = True
    deployment: str = DATA_CENTER


class JiraClient:
    # When Jira says "too many requests" (429), wait as long as it asks (Retry-After),
    # or back off 2, 4, 8... seconds, and keep trying until MAX_TOTAL_WAIT is used up.
    MAX_WAIT = 60.0  # seconds, for any single wait
    MAX_TOTAL_WAIT = 300.0  # seconds, for one request
    MAX_ATTEMPTS = 30  # in case Jira keeps asking for very short waits
    # After a 429, space later requests at least this far apart (or by the refill time
    # Jira's X-RateLimit headers give), so a scan doesn't empty the allowance again.
    MIN_GAP_AFTER_LIMIT = 1.0

    def __init__(self, creds: Credentials, timeout: float = 30.0):
        self.base_url = creds.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "https://" + self.base_url
        self.timeout = timeout
        self.deployment = creds.deployment
        self.api = "/rest/api/3" if self.is_cloud else "/rest/api/2"
        self._local = threading.local()  # per-thread wait notices
        self._pace_lock = threading.Lock()
        self._gap = 0.0  # seconds between requests, once Jira has limited us
        self._next_at = 0.0  # monotonic time the next request may go out
        self.session = requests.Session()
        self.session.verify = creds.verify_ssl
        self.session.headers.update({"Accept": "application/json"})
        if creds.use_token and not self.is_cloud:
            self.session.headers["Authorization"] = f"Bearer {creds.secret}"
        else:
            self.session.auth = (creds.username, creds.secret)

    def _request(self, method: str, path: str,
                 cancelled: Callable[[], bool] | None = None, **kwargs) -> dict | list:
        url = f"{self.base_url}{path}"
        waited, attempt = 0.0, 0
        while True:
            if not self._pace(cancelled):
                raise Cancelled("Cancelled while waiting for Jira's rate limit.", 429)
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.SSLError as exc:
                raise JiraError(
                    "SSL certificate check failed. If your company uses an internal "
                    "certificate, untick 'Verify SSL certificate' and try again."
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise JiraError(f"Could not reach Jira at {self.base_url}: {exc}") from exc
            self._learn_pace(resp)
            if resp.status_code != 429:
                break
            wait = max(_retry_after(resp, attempt, self.MAX_WAIT), _refill_time(resp) or 0.0)
            wait = min(wait, self.MAX_WAIT)
            if _blocked(resp) or waited + wait > self.MAX_TOTAL_WAIT or \
                    attempt + 1 >= self.MAX_ATTEMPTS:
                break
            if not self._wait(wait, cancelled):
                raise Cancelled("Cancelled while waiting for Jira's rate limit.", 429)
            waited += wait
            attempt += 1

        if resp.status_code == 429:
            raise JiraError(self._limit_error(resp, method, path, attempt, waited), 429)

        if resp.status_code in (401, 403):
            raise JiraError(self._auth_error(resp, method, path), resp.status_code)
        if not resp.ok:
            raise JiraError(f"Jira returned {resp.status_code}: {_error_text(resp)}",
                            resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise JiraError(
                "Jira returned a non-JSON response. Check the URL points to your Jira "
                "base address (for example https://jira.company.com)."
            ) from exc

    def _pace(self, cancelled: Callable[[], bool] | None) -> bool:
        """Once Jira has rate-limited this client, keep requests _gap seconds apart."""
        with self._pace_lock:
            if self._gap <= 0:
                return True
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self._gap
        return self._wait(start - now, cancelled, pacing=True)

    def _learn_pace(self, resp: requests.Response):
        refill = _refill_time(resp)
        limited = resp.status_code == 429
        remaining = _header_number(resp, "X-RateLimit-Remaining")
        if limited or (remaining is not None and remaining < 2 and refill):
            gap = min(max(refill or 0.0, self.MIN_GAP_AFTER_LIMIT), self.MAX_WAIT)
            with self._pace_lock:
                self._gap = max(self._gap, gap)

    def _limit_error(self, resp, method, path, attempts, waited) -> str:
        lines = ["Jira is limiting how many requests your account can make (429)."]
        detail = _error_text(resp)
        if detail:
            lines.append(f"Jira says: {detail}")
        if _blocked(resp):
            lines.append("Jira's rate limit for your account allows no requests at all, so "
                         "waiting won't help. Ask your Jira admin to check the rate limiting "
                         "settings for your account in Jira administration.")
        else:
            lines.append(f"The app waited {waited:.0f} s in total and tried {attempts + 1} "
                         "times. Wait a few minutes and try again; if it keeps happening, "
                         "ask your Jira admin to raise the rate limit for your account.")
        limit_headers = [f"{k}: {v}" for k, v in resp.headers.items()
                         if k.lower() == "retry-after" or k.lower().startswith("x-ratelimit")]
        if limit_headers:
            lines.append("Limit details from Jira: " + "; ".join(limit_headers))
        lines.append(f"(Request: {method} {path})")
        return "\n".join(lines)

    def notify_waits(self, callback: Callable[[float], None] | None):
        """Call `callback(seconds)` whenever a request on the calling thread has to wait
        for Jira's rate limit, e.g. to tell the user why nothing is happening."""
        self._local.on_wait = callback

    def _wait(self, seconds: float, cancelled: Callable[[], bool] | None,
              pacing: bool = False) -> bool:
        """Sleep in short steps so a cancel isn't held up. False if cancelled.
        Retry waits of 1 s or more are announced; pacing waits only when long."""
        notice = getattr(self._local, "on_wait", None)
        if notice and seconds >= (5 if pacing else 1):
            notice(seconds)
        end = time.monotonic() + seconds
        while True:
            if cancelled and cancelled():
                return False
            left = end - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(0.25, left))

    def _auth_error(self, resp: requests.Response, method: str, path: str) -> str:
        """Explain a 401/403, including Jira's own reason so the user can act on it."""
        if resp.status_code == 401:
            head = ("Login failed: check your email and API token." if self.is_cloud
                    else "Login failed: check your username and password/token.")
        else:
            head = "Access denied by Jira (403)."
        lines = [head]
        detail = _error_text(resp)
        if detail:
            lines.append(f"Jira says: {detail}")
        if resp.headers.get("X-Authentication-Denied-Reason") or \
                "CAPTCHA" in resp.headers.get("X-Seraph-LoginReason", "").upper():
            lines.append("Jira wants a CAPTCHA after failed logins: log out and back in "
                         "once in your browser, then try again.")
        lines.append(f"(Request: {method} {path})")
        return "\n".join(lines)

    @property
    def is_cloud(self) -> bool:
        return self.deployment == CLOUD

    def myself(self) -> dict:
        """Return the logged-in user; used to test the connection."""
        return self._request("GET", f"{self.api}/myself")

    def fields(self) -> list[dict]:
        """Return all fields (system and custom) visible to the user."""
        return self._request("GET", f"{self.api}/field")

    def project(self, key: str) -> dict:
        """Return a project by key or id; a clear error if it doesn't exist or isn't visible."""
        try:
            return self._request("GET", f"{self.api}/project/{quote(key, safe='')}")
        except JiraError as exc:
            if exc.status == 404:
                raise JiraError(f"Project '{key}' was not found, or you don't have "
                                "permission to see it.", 404) from exc
            raise

    def _paged_values(self, path: str, keys: tuple[str, ...],
                      cancelled: Callable[[], bool] | None = None) -> list[dict]:
        """Collect every page of a startAt/maxResults list endpoint."""
        out, start = [], 0
        while True:
            if cancelled and cancelled():
                return out
            try:
                data = self._request("GET", path, params={"startAt": start, "maxResults": 50},
                                     cancelled=cancelled)
            except Cancelled:
                return out
            items = next((data[k] for k in keys if isinstance(data.get(k), list)), [])
            out.extend(items)
            start += len(items)
            total = data.get("total")
            if not items or data.get("isLast") is True or (total is not None and start >= total):
                return out
            if data.get("isLast") is None and total is None and len(items) < 50:
                return out

    # Jira Software (agile) API: the same /rest/agile/1.0 paths on Cloud and Data Center.
    AGILE = "/rest/agile/1.0"

    def board(self, board_id: int) -> dict:
        """Return a board; a clear error if it doesn't exist or isn't visible."""
        try:
            return self._request("GET", f"{self.AGILE}/board/{int(board_id)}")
        except JiraError as exc:
            if exc.status == 404:
                raise JiraError(f"Board {board_id} was not found, or you don't have "
                                "permission to see it.", 404) from exc
            raise

    def board_sprints(self, board_id: int) -> list[dict]:
        """All sprints on a board (future, active and closed)."""
        return self._paged_values(f"{self.AGILE}/board/{int(board_id)}/sprint", ("values",))

    def sprint_report(self, board_id: int, sprint_id: int) -> dict:
        """The data behind Jira's own Sprint Report: issues completed, not completed and
        removed, which were added after the start, and their estimates then and now.

        This is the endpoint Jira's board UI uses (/rest/greenhopper/1.0). Atlassian
        doesn't document it as a public API, but Cloud and Data Center both serve it."""
        try:
            return self._request("GET", "/rest/greenhopper/1.0/rapid/charts/sprintreport",
                                 params={"rapidViewId": int(board_id),
                                         "sprintId": int(sprint_id)})
        except JiraError as exc:
            if exc.status == 404:
                raise JiraError(f"Jira has no sprint report for sprint {sprint_id} on board "
                                f"{board_id}, or this Jira doesn't offer sprint reports.",
                                404) from exc
            raise

    def scope_change_burndown(self, board_id: int, sprint_id: int) -> dict:
        """The data behind Jira's own Burndown Chart: every change to the sprint's issues
        (added or removed, re-estimated, done or reopened) with its time, and the sprint's
        start, end and completion times. Same undocumented /rest/greenhopper/1.0 family
        as sprint_report."""
        try:
            return self._request(
                "GET", "/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart",
                params={"rapidViewId": int(board_id), "sprintId": int(sprint_id)})
        except JiraError as exc:
            if exc.status == 404:
                raise JiraError(f"Jira has no burndown data for sprint {sprint_id} on board "
                                f"{board_id}, or this Jira doesn't offer burndown charts.",
                                404) from exc
            raise

    def create_sprint(self, name: str, board_id: int, start: str | None = None,
                      end: str | None = None, goal: str | None = None) -> dict:
        """Create a future sprint on a board. Dates are ISO 8601 strings with an offset."""
        body: dict = {"name": name, "originBoardId": int(board_id)}
        if start:
            body["startDate"] = start
        if end:
            body["endDate"] = end
        if goal:
            body["goal"] = goal
        return self._request("POST", f"{self.AGILE}/sprint", json=body)

    def create_issue_types(self, project_key: str,
                           cancelled: Callable[[], bool] | None = None) -> list[dict]:
        """Issue types a user can create in the project (each has 'id' and 'name')."""
        return self._paged_values(
            f"{self.api}/issue/createmeta/{quote(project_key, safe='')}/issuetypes",
            ("issueTypes", "values"), cancelled)

    def create_fields(self, project_key: str, issue_type_id: str,
                      cancelled: Callable[[], bool] | None = None) -> list[dict]:
        """Fields on the create screen for one issue type ('fieldId', 'name', 'schema')."""
        return self._paged_values(
            f"{self.api}/issue/createmeta/{quote(project_key, safe='')}/issuetypes/"
            f"{quote(issue_type_id, safe='')}",
            ("fields", "values"), cancelled)

    def search(
        self,
        jql: str,
        field_ids: list[str],
        page_size: int = 100,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[dict]:
        """Yield every issue matching `jql`, fetching page by page.

        `progress(done, total)` is called after each page; on Cloud `total` is
        Jira's approximate count (0 if unavailable).
        """
        search = self._search_cloud if self.is_cloud else self._search_dc
        return search(jql, field_ids, page_size, progress, cancelled)

    def _search_cloud(self, jql, field_ids, page_size, progress, cancelled):
        # Cloud removed /rest/api/3/search; /search/jql pages with nextPageToken
        # and returns no total, so the total comes from /search/approximate-count.
        try:
            total = int(self._request("POST", f"{self.api}/search/approximate-count",
                                      json={"jql": jql}).get("count", 0))
        except JiraError:
            total = 0
        done, token = 0, None
        while True:
            if cancelled and cancelled():
                return
            body = {"jql": jql, "maxResults": page_size, "fields": field_ids}
            if token:
                body["nextPageToken"] = token
            try:
                data = self._request("POST", f"{self.api}/search/jql", json=body,
                                     cancelled=cancelled)
            except Cancelled:
                return
            issues = data.get("issues", [])
            yield from issues
            done += len(issues)
            if progress:
                # The count is approximate, so never report fewer than fetched.
                progress(done, max(total, done) if total else 0)
            token = data.get("nextPageToken")
            if not issues or not token or data.get("isLast"):
                return

    def _search_dc(
        self,
        jql: str,
        field_ids: list[str],
        page_size: int = 100,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[dict]:
        """Yield every issue matching `jql`, fetching page by page."""
        start = 0
        while True:
            if cancelled and cancelled():
                return
            try:
                data = self._request(
                    "POST",
                    f"{self.api}/search",
                    json={
                        "jql": jql,
                        "startAt": start,
                        "maxResults": page_size,
                        "fields": field_ids,
                    },
                    cancelled=cancelled,
                )
            except Cancelled:
                return
            issues = data.get("issues", [])
            total = data.get("total", 0)
            yield from issues
            start += len(issues)
            if progress:
                progress(start, total)
            if not issues or start >= total:
                return


def _retry_after(resp: requests.Response, attempt: int, cap: float) -> float:
    """Seconds to wait before retrying a 429: Jira's Retry-After (seconds or an HTTP
    date) if it sent one, otherwise 2, 4, 8... seconds."""
    value = (resp.headers.get("Retry-After") or "").strip()
    wait = None
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                wait = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                wait = None
    if wait is None:
        wait = 2.0 ** (attempt + 1)
    return max(0.0, min(wait, cap))


def _header_number(resp: requests.Response, name: str) -> float | None:
    try:
        return float(resp.headers.get(name, "").strip())
    except ValueError:
        return None


def _refill_time(resp: requests.Response) -> float | None:
    """Seconds for one request's worth of allowance to come back, from the X-RateLimit
    headers Jira Data Center's rate limiting sends (FillRate requests per Interval)."""
    rate = _header_number(resp, "X-RateLimit-FillRate")
    interval = _header_number(resp, "X-RateLimit-Interval-Seconds")
    if rate and rate > 0:
        return (interval if interval and interval > 0 else 1.0) / rate
    return None


def _blocked(resp: requests.Response) -> bool:
    """The headers say the account may make no requests at all, so retrying is pointless."""
    return _header_number(resp, "X-RateLimit-Limit") == 0 or \
        _header_number(resp, "X-RateLimit-FillRate") == 0


def _error_text(resp: requests.Response) -> str:
    """Pull a readable message out of a Jira (or Atlassian gateway) error response."""
    try:
        body = resp.json()
    except ValueError:
        text = resp.text or ""
        if "<html" in text[:500].lower():  # an HTML error page says nothing useful here
            return ""
        return " ".join(text.split())[:300]
    if not isinstance(body, dict):
        return str(body)[:300]
    msgs = [str(m) for m in body.get("errorMessages") or []]
    errors = body.get("errors")
    if isinstance(errors, dict):
        msgs += [f"{k}: {v}" for k, v in errors.items()]
    for key in ("message", "error_description", "error"):  # gateway-style bodies
        if isinstance(body.get(key), str) and body[key] not in msgs:
            msgs.append(body[key])
    return "; ".join(msgs)[:500]
