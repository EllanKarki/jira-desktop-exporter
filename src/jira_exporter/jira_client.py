"""Minimal Jira Data Center REST client (API v2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

import requests


class JiraError(Exception):
    """Raised when Jira returns an error or cannot be reached."""


@dataclass
class Credentials:
    base_url: str
    username: str
    secret: str
    # When True, `secret` is a Personal Access Token sent as a Bearer token
    # (Jira Data Center 8.14+). Otherwise it is the user's password (Basic auth).
    use_token: bool = False
    verify_ssl: bool = True


class JiraClient:
    def __init__(self, creds: Credentials, timeout: float = 30.0):
        self.base_url = creds.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "https://" + self.base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = creds.verify_ssl
        self.session.headers.update({"Accept": "application/json"})
        if creds.use_token:
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

    def myself(self) -> dict:
        """Return the logged-in user; used to test the connection."""
        return self._request("GET", "/rest/api/2/myself")

    def fields(self) -> list[dict]:
        """Return all fields (system and custom) visible to the user."""
        return self._request("GET", "/rest/api/2/field")

    def search(
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
                "/rest/api/2/search",
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
