import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
]

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

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"errorMessages": ["nope"]})
        if self.path == "/rest/api/2/myself":
            return self._send(200, {"name": "alice", "displayName": "Alice A"})
        if self.path == "/rest/api/2/field":
            return self._send(200, FIELDS)
        self._send(404, {"errorMessages": ["not found"]})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["jql"] == "bad":
            return self._send(400, {"errorMessages": ["Error in the JQL Query"]})
        start, n = body["startAt"], body["maxResults"]
        self.server.requests.append(body)
        self._send(200, {"startAt": start, "maxResults": n, "total": len(ISSUES),
                         "issues": ISSUES[start:start + n]})


@pytest.fixture
def jira_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeJira)
    srv.requests = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
