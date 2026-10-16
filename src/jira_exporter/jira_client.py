"""Minimal Jira REST client for Data Center (API v2) and Cloud (API v3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import urlparse

import requests

DATA_CENTER = "datacenter"
CLOUD = "cloud"


class JiraError(Exception):
    """Raised when Jira returns an error or cannot be reached."""


def guess_deployment(url: str) -> str:
    """Atlassian-hosted sites live on *.atlassian.net; anything else is assumed self-hosted."""
    host = urlparse(url if "://" in url else "https://" + url).hostname or ""
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
    def __init__(self, creds: Credentials, timeout: float = 30.0):
        self.base_url = creds.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "https://" + self.base_url
        self.timeout = timeout
        self.deployment = creds.deployment
        self.api = "/rest/api/3" if self.is_cloud else "/rest/api/2"
        self.session = requests.Session()
        self.session.verify = creds.verify_ssl
        self.session.headers.update({"Accept": "application/json"})
        if creds.use_token and not self.is_cloud:
            self.session.headers["Authorization"] = f"Bearer {creds.secret}"
        else:
            self.session.auth = (creds.username, creds.secret)

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise JiraError(
                "SSL certificate check failed. If your company uses an internal "
                "certificate, untick 'Verify SSL certificate' and try again."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise JiraError(f"Could not reach Jira at {self.base_url}: {exc}") from exc

        if resp.status_code == 401:
            if self.is_cloud:
                raise JiraError("Login failed: check your email and API token.")
            raise JiraError("Login failed: check your username and password/token.")
        if resp.status_code == 403:
            hint = ""
            if resp.headers.get("X-Authentication-Denied-Reason"):
                hint = (" Jira asked for a CAPTCHA; log in once through the browser, "
                        "then try again.")
            raise JiraError(f"Access denied by Jira (403).{hint}")
        if not resp.ok:
            raise JiraError(f"Jira returned {resp.status_code}: {_error_text(resp)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise JiraError(
                "Jira returned a non-JSON response. Check the URL points to your Jira "
                "base address (for example https://jira.company.com)."
            ) from exc

    @property
    def is_cloud(self) -> bool:
        return self.deployment == CLOUD

    def myself(self) -> dict:
        """Return the logged-in user; used to test the connection."""
        return self._request("GET", f"{self.api}/myself")

    def fields(self) -> list[dict]:
        """Return all fields (system and custom) visible to the user."""
        return self._request("GET", f"{self.api}/field")

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
            data = self._request("POST", f"{self.api}/search/jql", json=body)
            issues = data.get("issues", [])
            yield from issues
            done += len(issues)
            if progress:
                progress(done, max(total, done))
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
            data = self._request(
                "POST",
                f"{self.api}/search",
                json={
                    "jql": jql,
                    "startAt": start,
                    "maxResults": page_size,
                    "fields": field_ids,
                },
            )
            issues = data.get("issues", [])
            total = data.get("total", 0)
            yield from issues
            start += len(issues)
            if progress:
                progress(start, total)
            if not issues or start >= total:
                return


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    msgs = list(body.get("errorMessages", []))
    msgs += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    return "; ".join(msgs) or resp.text[:300]
