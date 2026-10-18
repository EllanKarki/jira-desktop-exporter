"""'Create Sprint': the team sprint config, the Jira calls and the dialog."""
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import Workbook, load_workbook

from jira_exporter.jira_client import CLOUD, Credentials, JiraClient, JiraError
from jira_exporter.sprint_config import (
    COLUMNS,
    SprintConfigError,
    TeamConfig,
    jira_datetime,
    load_teams,
    plan_sprint,
    write_template,
)

ROOT = Path(__file__).resolve().parents[1]


def client(srv, deployment="datacenter"):
    return JiraClient(Credentials(f"http://127.0.0.1:{srv.server_address[1]}", "alice", "secret",
                                  deployment=deployment))


def write_rows(path, rows, header=COLUMNS):
    wb = Workbook()
    ws = wb.active
    ws.title = "Teams"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


ROWS = [
    ["Team Alpha", "ABC", 1, "", 1, date(2026, 1, 5), 14, time(9, 0), "Goal {number}"],
    ["Team Alpha", "XYZ", 3, "{project}-{number}", 10, date(2026, 3, 2), 7, None, ""],
    ["Team Beta", "ABC", 2, "Beta {number}", 1, "2026-01-12", 14, "10:30", ""],
    ["Team Kanban", "ABC", 7, "", 1, date(2026, 1, 5), 14, None, ""],
    ["Team Lost", "ABC", 99, "", 1, date(2026, 1, 5), 14, None, ""],
]


# ------------------------------------------------------------------- config ---

def test_shipped_template_loads():
    teams, problems = load_teams(ROOT / "config" / "sprints.xlsx")
    assert problems == []
    assert [(t.team, t.project_key) for t in teams] == [
        ("Team Alpha", "ABC"), ("Team Beta", "ABC"), ("Team Gamma", "XYZ")]
    gamma = teams[2]
    assert gamma.name_format == "{team} Sprint {number}" and gamma.start_time == time(9, 0)
    wb = load_workbook(ROOT / "config" / "sprints.xlsx")
    assert wb.sheetnames == ["Teams", "Help"]


def test_template_round_trip(tmp_path):
    write_template(tmp_path / "s.xlsx")
    teams, problems = load_teams(tmp_path / "s.xlsx")
    assert len(teams) == 3 and problems == []


def test_load_teams_parses_values(tmp_path):
    teams, problems = load_teams(write_rows(tmp_path / "s.xlsx", ROWS))
    assert problems == []
    alpha, alpha_xyz, beta = teams[:3]
    assert (alpha.board_id, alpha.first_start, alpha.length_days, alpha.goal) == \
        (1, date(2026, 1, 5), 14, "Goal {number}")
    assert alpha.name_format == "{team} Sprint {number}"
    assert alpha_xyz.start_time == time(9, 0)
    assert (beta.first_start, beta.start_time) == (date(2026, 1, 12), time(10, 30))


def test_load_teams_headers_ignore_case_and_order(tmp_path):
    header = ["sprint length (days)", "  team ", "BOARD ID", "Project Key",
              "First Sprint Number", "First  Sprint Start Date"]
    path = write_rows(tmp_path / "s.xlsx", [[14, "T", 5, "ABC", 3, date(2026, 2, 2)]], header)
    (team,), problems = load_teams(path)
    assert (team.team, team.board_id, team.first_number, team.length_days) == ("T", 5, 3, 14)


def test_load_teams_reports_bad_rows_and_keeps_good_ones(tmp_path):
    rows = [
        ROWS[0],
        ["", "ABC", 1, "", 1, date(2026, 1, 5), 14, None, ""],             # no team
        ["T2", "ABC", "x", "", 1, date(2026, 1, 5), 14, None, ""],         # bad board
        ["T3", "ABC", 1, "", 1, "01/05/2026", 14, None, ""],               # ambiguous date
        ["T4", "ABC", 1, "", 1, date(2026, 1, 5), 0, None, ""],            # zero length
        ["T5", "ABC", 1, "", 1, date(2026, 1, 5), 14, "noon", ""],         # bad time
        [None] * 9,                                                        # blank: ignored
        ["team alpha", "abc", 2, "", 1, date(2026, 1, 5), 14, None, ""],   # duplicate
    ]
    teams, problems = load_teams(write_rows(tmp_path / "s.xlsx", rows))
    assert [t.team for t in teams] == ["Team Alpha"]
    assert len(problems) == 6
    assert problems[0] == "Row 3: 'Team' is empty."
    assert "Row 4: 'Board ID' must be a whole number" in problems[1]
    assert "Row 5: 'First Sprint Start Date' must be a date" in problems[2]
    assert "Row 6: 'Sprint Length (days)' must be 1 or more" in problems[3]
    assert "Row 7: 'Start Time'" in problems[4]
    assert "Row 9: team alpha / abc is listed more than once" in problems[5]


def test_load_teams_missing_columns(tmp_path):
    path = write_rows(tmp_path / "s.xlsx", [["T", "ABC"]], ["Team", "Project Key"])
    with pytest.raises(SprintConfigError, match="missing column.*Board ID"):
        load_teams(path)


def test_load_teams_missing_or_bad_file(tmp_path):
    with pytest.raises(SprintConfigError, match="not found"):
        load_teams(tmp_path / "nope.xlsx")
    (tmp_path / "bad.xlsx").write_text("not excel")
    with pytest.raises(SprintConfigError, match="not a readable .xlsx"):
        load_teams(tmp_path / "bad.xlsx")


def _cfg(**kw):
    base = dict(team="Team Alpha", project_key="ABC", board_id=1, first_number=10,
                first_start=date(2026, 1, 5), length_days=14)
    return TeamConfig(**{**base, **kw})


def test_plan_sprint_dates_follow_on():
    p = plan_sprint(_cfg(), 10)
    assert (p.name, p.start, p.end) == ("Team Alpha Sprint 10", datetime(2026, 1, 5, 9),
                                        datetime(2026, 1, 19, 9))
    nxt = plan_sprint(_cfg(), 11)
    assert nxt.start == p.end
    earlier = plan_sprint(_cfg(), 8)
    assert earlier.start == datetime(2025, 12, 8, 9)


def test_plan_sprint_placeholders():
    cfg = _cfg(name_format="{project} {team} #{number}", goal="Ship {project} {number}",
               start_time=time(13, 15))
    p = plan_sprint(cfg, 12)
    assert (p.name, p.goal, p.start.time()) == ("ABC Team Alpha #12", "Ship ABC 12", time(13, 15))
    with pytest.raises(SprintConfigError, match="placeholders"):
        plan_sprint(_cfg(name_format="{sprint}"), 1)
    with pytest.raises(SprintConfigError, match="1 or more"):
        plan_sprint(_cfg(), 0)


def test_jira_datetime():
    aware = datetime(2026, 1, 5, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert jira_datetime(aware) == "2026-01-05T09:00:00.000+05:30"
    local = jira_datetime(datetime(2026, 1, 5, 9, 0))
    assert local.startswith("2026-01-05T09:00:00.000") and local[-6] in "+-"


# ------------------------------------------------------------------ client ---

@pytest.mark.parametrize("cloud", [False, True])
def test_client_board_sprints_and_create(jira_server, cloud_server, cloud):
    srv = cloud_server if cloud else jira_server
    c = client(srv, CLOUD if cloud else "datacenter")
    assert c.board(1)["type"] == "scrum"
    assert [s["name"] for s in c.board_sprints(1)] == [f"Team Alpha Sprint {i:02d}" for i in (1, 2, 3)]
    pages = [p for p, _ in srv.requests if p.startswith("/rest/agile/1.0/board/1/sprint")]
    assert len(pages) == 2  # followed the paging
    made = c.create_sprint("Team Alpha Sprint 4", 1, "2026-02-02T09:00:00.000+05:30",
                           "2026-02-16T09:00:00.000+05:30", "Goal")
    assert made["state"] == "future" and made["name"] == "Team Alpha Sprint 4"
    path, body = srv.requests[-1]
    assert path == "/rest/agile/1.0/sprint"
    assert body == {"name": "Team Alpha Sprint 4", "originBoardId": 1,
                    "startDate": "2026-02-02T09:00:00.000+05:30",
                    "endDate": "2026-02-16T09:00:00.000+05:30", "goal": "Goal"}
    assert c.create_sprint("Bare", 2) and srv.requests[-1][1] == {"name": "Bare",
                                                                  "originBoardId": 2}


def test_client_unknown_board(jira_server):
    with pytest.raises(JiraError, match="Board 99 was not found"):
        client(jira_server).board(99)


# ------------------------------------------------------------------ dialog ---

@pytest.fixture
def dialog(jira_server, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    from jira_exporter.sprint_dialog import SprintDialog

    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path))
    qapp = QApplication.instance() or QApplication([])
    settings = QSettings("JiraDesktopExporter", "JiraDesktopExporter")
    settings.setValue("sprint_config", str(write_rows(tmp_path / "teams.xlsx", ROWS)))
    asked = []

    def question(_parent, _title, text, *a, **k):
        asked.append(text)
        return QMessageBox.Yes if not getattr(question, "say_no", False) else QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", question)
    dlg = SprintDialog(client(jira_server), settings, None)
    dlg.show()
    yield qapp, dlg, asked, question
    dlg.reject()


def _pick(dlg, team, project=None):
    dlg.team.setCurrentText(team)
    if project:
        dlg.project.setCurrentText(project)


def _create(qapp, dlg):
    from test_ui import wait_until
    dlg.create_btn.click()
    wait_until(qapp, lambda: dlg._worker is None)


def test_dialog_lists_teams_and_projects(dialog):
    qapp, dlg, _, _ = dialog
    assert [dlg.team.itemText(i) for i in range(dlg.team.count())] == \
        ["Team Alpha", "Team Beta", "Team Kanban", "Team Lost"]
    assert [dlg.project.itemText(i) for i in range(dlg.project.count())] == ["ABC", "XYZ"]
    assert dlg.name.text() == "Team Alpha Sprint 01"
    dlg.number.setText("04")
    assert dlg.name.text() == "Team Alpha Sprint 04"
    assert dlg.goal.text() == "Goal 04"
    assert dlg.start.dateTime().toPython() == datetime(2026, 2, 16, 9)
    assert dlg.end.dateTime().toPython() == datetime(2026, 3, 2, 9)
    dlg.project.setCurrentText("XYZ")
    assert (dlg.number.text(), dlg.name.text()) == ("10", "XYZ-10")
    assert dlg.start.dateTime().toPython() == datetime(2026, 3, 2, 9)
    _pick(dlg, "Team Beta")
    assert dlg.number.text() == "01" and dlg.name.text() == "Beta 01"
    assert dlg.start.dateTime().toPython() == datetime(2026, 1, 12, 10, 30)
    assert "5 team/project rows loaded" in dlg.status.text()


def test_dialog_creates_sprint_after_confirming(dialog, jira_server):
    qapp, dlg, asked, _ = dialog
    dlg.number.setText("04")
    dlg.goal.setText("Edited goal")
    _create(qapp, dlg)
    assert "Created sprint 'Team Alpha Sprint 04'" in dlg.status.text(), dlg.status.text()
    assert "Name: Team Alpha Sprint 04" in asked[0] and "Board: ABC Alpha board (1)" in asked[0]
    path, body = jira_server.requests[-1]
    assert path == "/rest/agile/1.0/sprint"
    assert body["name"] == "Team Alpha Sprint 04" and body["originBoardId"] == 1
    assert body["goal"] == "Edited goal"
    assert body["startDate"].startswith("2026-02-16T09:00:00.000")
    assert body["endDate"].startswith("2026-03-02T09:00:00.000")
    assert dlg.created["state"] == "future"
    assert dlg.number.text() == "05" and dlg.name.text() == "Team Alpha Sprint 05"
    assert dlg.settings.value("sprint_team") == "Team Alpha"


def test_dialog_refuses_existing_sprint_name(dialog, jira_server):
    qapp, dlg, asked, _ = dialog
    dlg.number.setText("02")
    _create(qapp, dlg)
    assert "already has a closed sprint called 'Team Alpha Sprint 02'" in dlg.status.text()
    assert asked == []
    assert not any(p == "/rest/agile/1.0/sprint" for p, _ in jira_server.requests)
    assert dlg.create_btn.isEnabled()


def test_dialog_rejects_kanban_and_unknown_boards(dialog, jira_server):
    qapp, dlg, asked, _ = dialog
    _pick(dlg, "Team Kanban")
    _create(qapp, dlg)
    assert "is a Kanban board" in dlg.status.text()
    _pick(dlg, "Team Lost")
    _create(qapp, dlg)
    assert "Board 99 was not found" in dlg.status.text()
    assert asked == []


def test_dialog_warns_when_board_is_in_another_project(dialog, jira_server, tmp_path):
    qapp, dlg, asked, question = dialog
    question.say_no = True
    rows = [["Team X", "ABC", 3, "", 1, date(2026, 1, 5), 14, None, ""]]
    dlg.config_path.setText(str(write_rows(tmp_path / "other.xlsx", rows)))
    dlg.load_config()
    _create(qapp, dlg)
    assert "located in project XYZ, not ABC" in asked[0]
    assert dlg.status.text() == "Not created."
    assert not any(p == "/rest/agile/1.0/sprint" for p, _ in jira_server.requests)


def test_dialog_checks_form_before_calling_jira(dialog, jira_server):
    qapp, dlg, asked, _ = dialog
    dlg.name.setText("  ")
    dlg.create_btn.click()
    assert dlg.status.text() == "Enter a sprint name."
    dlg.name.setText("A sprint name that is too long, 37")
    dlg.create_btn.click()
    assert "up to 30 characters" in dlg.status.text()
    dlg.number.setText("05")
    dlg.end.setDateTime(dlg.start.dateTime())
    dlg.create_btn.click()
    assert dlg.status.text() == "The end must be after the start."
    assert dlg._worker is None and jira_server.requests == []


def test_dialog_missing_config_offers_template(dialog, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    qapp, dlg, _, _ = dialog
    dlg.config_path.setText(str(tmp_path / "missing.xlsx"))
    dlg.load_config()
    assert "not found" in dlg.status.text() and "New from template" in dlg.status.text()
    assert dlg.team.count() == 0 and not dlg.create_btn.isEnabled()
    target = tmp_path / "mine"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    dlg.findChildren(type(dlg.create_btn))[1].click()  # "New from template..."
    assert (tmp_path / "mine.xlsx").is_file()
    assert dlg.team.count() == 3 and "Example config saved" in dlg.status.text()


def test_dialog_close_while_checking(jira_server, dialog):
    qapp, dlg, _, _ = dialog
    dlg.create_btn.click()
    assert dlg._worker is not None
    dlg.reject()  # must not block or crash; the late result is ignored
    assert dlg._worker is None
    from jira_exporter import sprint_dialog
    from test_ui import wait_until
    wait_until(qapp, lambda: all(not t.is_alive() for t, _ in sprint_dialog._WORKERS))
    for _ in range(20):
        qapp.processEvents()
    assert "Checking" in dlg.status.text()  # the result arrived after closing: ignored


def test_dialog_remembers_next_number_per_team_and_project(dialog, jira_server):
    qapp, dlg, _, _ = dialog
    dlg.number.setText("04")
    _create(qapp, dlg)
    assert dlg.number.text() == "05"
    _pick(dlg, "Team Beta")
    dlg.number.setText("50")
    _pick(dlg, "Team Alpha", "XYZ")
    assert dlg.number.text() == "10"  # XYZ's own first number, not Beta's 50
    dlg.project.setCurrentText("ABC")
    assert (dlg.number.text(), dlg.name.text()) == ("05", "Team Alpha Sprint 05")
    _pick(dlg, "Team Beta")
    assert dlg.number.text() == "01"


def test_dialog_reopens_on_last_team_and_project(dialog, jira_server):
    from jira_exporter.sprint_dialog import SprintDialog
    qapp, dlg, _, _ = dialog
    _pick(dlg, "Team Alpha", "XYZ")
    dlg.number.setText("11")
    _create(qapp, dlg)
    again = SprintDialog(dlg.client, dlg.settings, None)
    assert (again.team.currentText(), again.project.currentText()) == ("Team Alpha", "XYZ")
    assert again.number.text() == "12"
    again.reject()


def test_dialog_bad_name_format_clears_the_form(dialog, tmp_path):
    qapp, dlg, _, _ = dialog
    rows = [ROWS[0], ["Team Bad", "ABC", 1, "{team[x]}", 1, date(2026, 1, 5), 14, None, ""]]
    dlg.config_path.setText(str(write_rows(tmp_path / "bad.xlsx", rows)))
    dlg.load_config()
    assert dlg.name.text() == "Team Alpha Sprint 01"
    _pick(dlg, "Team Bad")
    assert "placeholders" in dlg.status.text()
    assert dlg.name.text() == "" and not dlg.create_btn.isEnabled()
    _pick(dlg, "Team Alpha")
    assert dlg.create_btn.isEnabled()


def test_sprint_number_keeps_leading_zeros():
    from jira_exporter.sprint_config import next_label, sprint_label
    assert [sprint_label(x) for x in (1, "1", "01", "001", " 7 ", "10", 123)] == \
        ["01", "01", "01", "001", "07", "10", "123"]
    assert [next_label(x) for x in ("01", "09", "99", "001", "1")] == \
        ["02", "10", "100", "002", "02"]
    for bad in ("", "0", "00", "1a", "-1", "１"):
        with pytest.raises(SprintConfigError):
            sprint_label(bad)
    p = plan_sprint(_cfg(first_number=1, goal="Goal {number}"), "03")
    assert (p.name, p.goal, p.start) == ("Team Alpha Sprint 03", "Goal 03", datetime(2026, 2, 2, 9))
    assert plan_sprint(_cfg(), "0010").name == "Team Alpha Sprint 0010"


def test_dialog_number_is_text_with_leading_zeros(dialog, jira_server):
    qapp, dlg, _, _ = dialog
    assert dlg.number.text() == "01"
    dlg.number.setText("7")
    assert dlg.name.text() == "Team Alpha Sprint 07" and dlg.goal.text() == "Goal 07"
    dlg.number.editingFinished.emit()
    assert dlg.number.text() == "07"
    dlg.number.setText("")
    assert "Enter the sprint number" in dlg.status.text() and not dlg.create_btn.isEnabled()
    dlg.number.setText("09")
    _create(qapp, dlg)
    assert jira_server.requests[-1][1]["name"] == "Team Alpha Sprint 09"
    assert dlg.number.text() == "10" and dlg.name.text() == "Team Alpha Sprint 10"
    from PySide6.QtGui import QValidator
    assert dlg.number.validator().validate("1a", 0)[0] == QValidator.Invalid


def test_plan_sprint_far_out_numbers():
    with pytest.raises(SprintConfigError, match="outside the dates"):
        plan_sprint(_cfg(length_days=10**6), 99999)


# --------------------------------------------------------------------- menu ---

def test_menu_opens_create_sprint_only_when_logged_in(jira_server, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from jira_exporter import app as appmod
    from test_ui import wait_until

    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path))
    qapp = QApplication.instance() or QApplication([])
    win = appmod.MainWindow()
    titles = [a.text() for a in win.menuBar().actions()]
    assert titles == ["&Jira", "&Configuration", "E&xtract", "Jira &Actions",
                      "&Reports"]
    assert not win.sprint_action.isEnabled()
    win.sprint_action.trigger()
    assert win.sprint_dialog is None

    win.login_action.trigger()
    win.login.url.setText(f"http://127.0.0.1:{jira_server.server_address[1]}")
    win.login.username.setText("alice")
    win.login.password.setText("secret")
    win.login.connect_btn.click()
    wait_until(qapp, lambda: win.export.client is not None)
    assert win.sprint_action.isEnabled()
    win.sprint_action.trigger()
    dlg = win.sprint_dialog
    assert dlg is not None and dlg.isVisible() and dlg.client is win.export.client
    win.sprint_action.trigger()
    assert win.sprint_dialog is dlg  # one at a time

    win.logout_action.trigger()
    wait_until(qapp, lambda: win.sprint_dialog is None)
    assert not win.sprint_action.isEnabled()
    win.close()
