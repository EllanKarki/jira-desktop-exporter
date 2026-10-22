import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FIELDS = [
    {"id": "issuekey", "name": "Key", "schema": None},
    {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
    {"id": "status", "name": "Status", "schema": {"type": "status"}},
    {"id": "assignee", "name": "Assignee", "schema": {"type": "user"}},
    {"id": "created", "name": "Created", "schema": {"type": "datetime"}},
    {"id": "duedate", "name": "Due Date", "schema": {"type": "date"}},
    {"id": "labels", "name": "Labels", "schema": {"type": "array"}},
    {"id": "customfield_10002", "name": "Story Points", "schema": {"type": "number"}},
    {"id": "description", "name": "Description", "schema": {"type": "string"}},
    {"id": "customfield_10003", "name": "Severity", "custom": True, "schema": {"type": "option"}},
    {"id": "customfield_10099", "name": "Old Unused Field", "custom": True, "schema": {"type": "string"}},
]

PROJECT = {"id": "10000", "key": "ABC", "name": "Alpha Beta"}
# Create screens per issue type: Severity is offered on Bug but never filled in.
CREATE_SCREENS = {
    "1": ("Bug", ["summary", "description", "duedate", "customfield_10003"]),
    "2": ("Story", ["summary", "labels", "customfield_10002"]),
}

# Agile boards: 1 and 2 are Scrum boards in ABC, 3 is in XYZ, 7 is a Kanban board.
BOARDS = {
    1: {"id": 1, "name": "ABC Alpha board", "type": "scrum",
        "location": {"projectKey": "ABC", "projectId": 10000}},
    2: {"id": 2, "name": "ABC Beta board", "type": "scrum",
        "location": {"projectKey": "ABC", "projectId": 10000}},
    3: {"id": 3, "name": "XYZ board", "type": "scrum",
        "location": {"projectKey": "XYZ", "projectId": 10001}},
    7: {"id": 7, "name": "Support kanban", "type": "kanban"},
}


def initial_sprints():
    return {1: [{"id": 100 + i, "name": f"Team Alpha Sprint {i:02d}", "originBoardId": 1,
                 "state": "closed" if i < 3 else "active"} for i in range(1, 4)]}


def _est(points):
    return {"statFieldId": "customfield_10002",
            "statFieldValue": {"value": points} if points is not None else {}}


def _ri(key, start, now=None):
    """A sprint report issue with its estimate at the start and now."""
    return {"key": key, "estimateStatistic": _est(start),
            "currentEstimateStatistic": _est(start if now is None else now)}


# Jira's sprint report per (board, sprint). Sprint 101 started with ABC-1..5 (21 points);
# ABC-6 was added and finished, ABC-7 was added then removed, ABC-5 was removed, ABC-1
# was re-estimated from 5 to 8 before it was done, and ABC-3/ABC-4 were left unfinished.
SPRINT_REPORTS = {
    (1, 101): {"contents": {
        "completedIssues": [_ri("ABC-1", 5, 8), _ri("ABC-2", 3), _ri("ABC-6", 2)],
        "issuesNotCompletedInCurrentSprint": [_ri("ABC-3", 5), _ri("ABC-4", None)],
        "puntedIssues": [_ri("ABC-5", 8), _ri("ABC-7", 1)],
        "issuesCompletedInAnotherSprint": [_ri("ABC-9", 13)],
        "issueKeysAddedDuringSprint": {"ABC-6": True, "ABC-7": True},
    }, "sprint": {"id": 101, "name": "Team Alpha Sprint 01", "state": "CLOSED"}},
    (1, 102): {"contents": {
        "completedIssues": [_ri("ABC-3", 5), _ri("ABC-4", 3)],
        "issuesNotCompletedInCurrentSprint": [],
        "puntedIssues": [],
        "issueKeysAddedDuringSprint": {},
    }, "sprint": {"id": 102, "name": "Team Alpha Sprint 02", "state": "CLOSED"}},
}


def local_ms(*args):
    """Milliseconds since 1970 for a local date and time, as Jira sends them."""
    from datetime import datetime
    return int(datetime(*args).timestamp() * 1000)


def _chg(key, added=None, est=None, done=None):
    c = {"key": key}
    if added is not None:
        c["added"] = added
    if est is not None:
        c["statC"] = {"newValue": est}
    if done is not None:
        c["column"] = {"done": done, "notDone": not done, "newStatus": "1"}
    return c


# Jira's scope change data per (board, sprint). Sprint 101 runs 5-9 January 2026 and
# starts with ABC-1 (5), ABC-2 (3) and ABC-3 (8): 16 points.
BURNDOWNS = {
    (1, 101): {
        "startTime": local_ms(2026, 1, 5, 9), "endTime": local_ms(2026, 1, 9, 9),
        "completeTime": local_ms(2026, 1, 9, 8), "now": local_ms(2026, 2, 1, 12),
        "changes": {
            str(local_ms(2026, 1, 5, 8)): [_chg("ABC-1", True, 5), _chg("ABC-2", True, 3),
                                           _chg("ABC-3", True, 8)],
            str(local_ms(2026, 1, 6, 10)): [_chg("ABC-1", done=True)],
            str(local_ms(2026, 1, 7, 11)): [_chg("ABC-4", True, 2)],
            str(local_ms(2026, 1, 7, 15)): [_chg("ABC-3", est=5)],
            str(local_ms(2026, 1, 8, 9)): [_chg("ABC-2", done=True)],
            str(local_ms(2026, 1, 8, 12)): [_chg("ABC-2", done=False)],
            str(local_ms(2026, 1, 8, 13)): [_chg("ABC-2", done=True)],
            str(local_ms(2026, 1, 8, 16)): [_chg("ABC-4", False)],
        },
    },
}


ADF_DESC = {"type": "doc", "version": 1, "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                      {"type": "text", "text": "world", "marks": [{"type": "strong"}]}]},
    {"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
    ]},
]}

ISSUES = [
    {
        "key": f"ABC-{i}",
        "fields": {
            "summary": f"Issue {i}",
            "status": {"name": "Open" if i % 2 else "Done"},
            "assignee": {"displayName": "Dev One", "name": "dev1"} if i % 3 else None,
            "created": "2026-01-02T10:15:30.000+0530",
            "duedate": "2026-02-01",
            "labels": ["a", "b"],
            "customfield_10002": i,
            "description": "Plain description",
        },
    }
    for i in range(1, 251)
]


class FakeJira(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        import base64
        h = self.headers.get("Authorization", "")
        if h == "Bearer good-token":
            return True
        if h.startswith("Basic "):
            return base64.b64decode(h[6:]).decode() == "alice:secret"
        return False

    def _rate_limited(self):
        """Answer 429 while the server's rate_limit counter lasts (like Jira's limiter)."""
        srv = self.server
        with srv.lock:
            if srv.rate_limit <= 0:
                return False
            srv.rate_limit -= 1
            srv.limited.append(self.path)
        data = json.dumps({"message": "Rate limit exceeded"}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        if srv.retry_after is not None:
            self.send_header("Retry-After", srv.retry_after)
        for k, v in srv.limit_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"errorMessages": ["nope"]})
        if self._rate_limited():
            return
        cloud = self.server.cloud
        if self.path == ("/rest/api/3/myself" if cloud else "/rest/api/2/myself"):
            if cloud:
                return self._send(200, {"accountId": "5b10ac", "displayName": "Alice Cloud"})
            return self._send(200, {"name": "alice", "displayName": "Alice A"})
        if self.path == ("/rest/api/3/field" if cloud else "/rest/api/2/field"):
            return self._send(200, FIELDS)
        url = urlparse(self.path)
        if url.path == "/rest/greenhopper/1.0/rapid/charts/sprintreport":
            q = parse_qs(url.query)
            self.server.requests.append((self.path, None))
            report = SPRINT_REPORTS.get((int(q["rapidViewId"][0]), int(q["sprintId"][0])))
            if report is None:
                return self._send(404, {"errorMessages": ["not found"]})
            return self._send(200, report)
        if url.path == "/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart":
            q = parse_qs(url.query)
            self.server.requests.append((self.path, None))
            data = BURNDOWNS.get((int(q["rapidViewId"][0]), int(q["sprintId"][0])))
            if data is None:
                return self._send(404, {"errorMessages": ["not found"]})
            return self._send(200, data)
        if url.path.startswith("/rest/agile/1.0/"):
            return self._agile_get(url.path.split("/")[4:], parse_qs(url.query))
        parts = url.path.split("/")[4:]  # after /rest/api/<n>/
        if url.path.startswith("/rest/api/3/" if cloud else "/rest/api/2/"):
            if parts[:1] == ["project"] and len(parts) == 2:
                if parts[1] in (PROJECT["key"], PROJECT["id"]):
                    return self._send(200, PROJECT)
                return self._send(404, {"errorMessages": ["No project could be found"]})
            if parts[:2] == ["issue", "createmeta"] and len(parts) in (4, 5):
                return self._createmeta(parts, parse_qs(url.query), cloud)
        self._send(404, {"errorMessages": ["not found"]})

    def _agile_get(self, parts, query):
        if parts[:1] == ["board"] and len(parts) in (2, 3) and parts[1].isdigit():
            board = BOARDS.get(int(parts[1]))
            if board is None:
                return self._send(404, {"errorMessages": ["Board does not exist or you do not "
                                                          "have permission to see it."]})
            if len(parts) == 2:
                return self._send(200, board)
            if parts[2] == "sprint":
                if board["type"] != "scrum":
                    return self._send(400, {"errorMessages": ["The board does not support "
                                                              "sprints"]})
                items = self.server.sprints.get(board["id"], [])
                start = int(query.get("startAt", ["0"])[0])
                n = min(2, int(query.get("maxResults", ["50"])[0]))  # force paging
                self.server.requests.append((self.path, None))
                return self._send(200, {"startAt": start, "maxResults": n,
                                        "isLast": start + n >= len(items),
                                        "values": items[start:start + n]})
        self._send(404, {"errorMessages": ["not found"]})

    def _create_sprint(self, body):
        self.server.requests.append((self.path, body))
        board = BOARDS.get(body.get("originBoardId"))
        if board is None or board["type"] != "scrum":
            return self._send(400, {"errorMessages": ["Board not found or not a Scrum board"]})
        if not body.get("name"):
            return self._send(400, {"errors": {"name": "Sprint name is required."}})
        sprint = {"id": 500 + sum(len(v) for v in self.server.sprints.values()),
                  "self": "http://jira/rest/agile/1.0/sprint/x", "state": "future", **body}
        self.server.sprints.setdefault(board["id"], []).append(sprint)
        self._send(201, sprint)

    def _createmeta(self, parts, query, cloud):
        if parts[2] != PROJECT["key"] or parts[3] != "issuetypes":
            return self._send(404, {"errorMessages": ["not found"]})
        if self.server.createmeta_status:
            return self._send(self.server.createmeta_status, {"errorMessages": ["createmeta off"]})
        if len(parts) == 4:
            items = [{"id": k, "name": v[0], "subtask": False} for k, v in CREATE_SCREENS.items()]
            key = "issueTypes" if cloud else "values"
        else:
            if parts[4] not in CREATE_SCREENS:
                return self._send(404, {"errorMessages": ["no such issue type"]})
            names = {f["id"]: f["name"] for f in FIELDS}
            items = [{"fieldId": fid, "key": fid, "name": names[fid], "required": fid == "summary"}
                     for fid in CREATE_SCREENS[parts[4]][1]]
            key = "fields" if cloud else "values"
        # Serve at most 2 per page so the client has to follow the paging.
        start = int(query.get("startAt", ["0"])[0])
        n = min(2, int(query.get("maxResults", ["50"])[0]))
        page = {"startAt": start, "maxResults": n, "total": len(items), key: items[start:start + n]}
        if not cloud:
            page["isLast"] = start + n >= len(items)
        self.server.requests.append((self.path, None))
        self._send(200, page)

    def do_POST(self):
        if not self._authed():
            return self._send(401, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self._rate_limited():
            return
        if self.path == "/rest/agile/1.0/sprint":
            return self._create_sprint(body)
        if self.server.search_delay:
            import time
            time.sleep(self.server.search_delay)
        if body["jql"] == "bad":
            return self._send(400, {"errorMessages": ["Error in the JQL Query"]})
        self.server.requests.append((self.path, body))
        if self.server.cloud:
            return self._post_cloud(body)
        if self.path != "/rest/api/2/search":
            return self._send(404, {})
        start, n = body["startAt"], body["maxResults"]
        self._send(200, {"startAt": start, "maxResults": n, "total": len(ISSUES),
                         "issues": ISSUES[start:start + n]})

    def _post_cloud(self, body):
        # Mirrors Jira Cloud: the old /search is gone, /search/jql pages by token
        # without a total, and /search/approximate-count gives the count.
        if self.path == "/rest/api/3/search/approximate-count":
            return self._send(200, {"count": len(ISSUES)})
        if self.path != "/rest/api/3/search/jql":
            return self._send(410, {"errorMessages": ["The requested API has been removed."]})
        start = int(body.get("nextPageToken") or 0)
        n = body["maxResults"]
        page = [dict(i, fields=dict(i["fields"], description=ADF_DESC))
                for i in ISSUES[start:start + n]]
        resp = {"issues": page, "isLast": start + n >= len(ISSUES)}
        if not resp["isLast"]:
            resp["nextPageToken"] = str(start + n)
        self._send(200, resp)


def _serve(cloud):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeJira)
    srv.requests = []
    srv.cloud = cloud
    srv.createmeta_status = None  # set to e.g. 404 to mimic Jira before 8.4
    srv.search_delay = 0  # seconds, to mimic a slow Jira
    srv.sprints = initial_sprints()
    srv.lock = threading.Lock()
    srv.rate_limit = 0  # answer this many requests with 429 first
    srv.retry_after = "0"  # Retry-After header for those, or None to send none
    srv.limited = []  # paths that got a 429
    srv.limit_headers = {}  # extra headers on those, e.g. X-RateLimit-FillRate
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def jira_server():
    srv = _serve(cloud=False)
    yield srv
    srv.shutdown()


@pytest.fixture
def cloud_server():
    srv = _serve(cloud=True)
    yield srv
    srv.shutdown()
