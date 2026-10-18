"""'Create Sprint' dialog: pick a team, project and sprint number; create the sprint in Jira."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtCore import QDateTime, QObject, QRegularExpression, QSettings, Qt, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from jira_exporter.config import default_config_path
from jira_exporter.jira_client import JiraClient, JiraError
from jira_exporter.sprint_config import (
    SprintConfigError,
    SprintPlan,
    TeamConfig,
    jira_datetime,
    load_teams,
    next_label,
    plan_sprint,
    sprint_label,
    write_template,
)


# Jira's documented limit (JSWCLOUD-17483, JSWSERVER-16256).
MAX_NAME = 30


def default_sprint_config_path() -> Path:
    return default_config_path().parent / "sprints.xlsx"


class SprintWorker(QObject):
    """check: look at the board and its sprints. create: create the sprint.

    Runs on a daemon Python thread rather than a QThread: a Jira request can't be
    interrupted, and a daemon thread can't hold up or crash the app when it quits
    mid-request. The worker object stays in the GUI thread, so its signals reach the
    dialog queued."""

    finished = Signal(str, object)  # mode, result
    failed = Signal(str)

    def __init__(self, client: JiraClient, mode: str, plan: SprintPlan, project_key: str):
        super().__init__()
        self.client, self.mode, self.plan, self.project_key = client, mode, plan, project_key

    def run(self):
        try:
            result = self._check() if self.mode == "check" else self._create()
            self.finished.emit(self.mode, result)
        except JiraError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep the dialog alive on anything unexpected
            self.failed.emit(f"Unexpected error: {exc}")

    def _check(self) -> dict:
        board = self.client.board(self.plan.board_id)
        name = board.get("name", str(self.plan.board_id))
        kind = (board.get("type") or "").lower()
        if kind == "kanban":  # other types are left for Jira to accept or refuse
            raise JiraError(f"Board {self.plan.board_id} ('{name}') is a Kanban board. "
                            "Sprints need a Scrum board; fix the Board ID in the sprint config.")
        warnings = []
        location = (board.get("location") or {}).get("projectKey")
        if location and location.lower() != self.project_key.lower():
            warnings.append(f"Board '{name}' is located in project {location}, not "
                            f"{self.project_key}.")
        wanted = self.plan.name.strip().lower()
        for sprint in self.client.board_sprints(self.plan.board_id):
            if (sprint.get("name") or "").strip().lower() == wanted:
                state = sprint.get("state", "existing")
                raise JiraError(f"Board '{name}' already has a {state} sprint called "
                                f"'{sprint.get('name')}'. Pick another sprint number or name.")
        return {"board_name": name, "warnings": warnings}

    def _create(self) -> dict:
        p = self.plan
        return self.client.create_sprint(p.name, p.board_id, jira_datetime(p.start),
                                         jira_datetime(p.end), p.goal or None)


class SprintDialog(QDialog):
    def __init__(self, client: JiraClient, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Sprint")
        self.resize(620, 420)
        self.client, self.settings = client, settings
        self.teams: list[TeamConfig] = []
        self._worker: SprintWorker | None = None
        self._plan_ok = False
        self._pending: tuple[SprintPlan, TeamConfig] | None = None
        self.created: dict | None = None  # last sprint Jira created

        self.config_path = QLineEdit(settings.value("sprint_config",
                                                    str(default_sprint_config_path())))
        self.config_path.editingFinished.connect(self.load_config)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)
        self.template = QPushButton("New from template...")
        self.template.setToolTip("Save an example sprint config to fill in")
        self.template.clicked.connect(self._new_template)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.config_path, 1)
        cfg_row.addWidget(self.browse)
        cfg_row.addWidget(self.template)

        self.team = QComboBox()
        self.team.currentIndexChanged.connect(self._team_changed)
        self.project = QComboBox()
        self.project.currentIndexChanged.connect(self._project_changed)
        # Text, not a spin box, so leading zeros are kept: 01, 02 ...
        self.number = QLineEdit()
        self.number.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}")))
        self.number.setPlaceholderText("e.g. 01")
        self.number.setMaximumWidth(120)
        self.number.textChanged.connect(self._refresh_plan)
        self.number.editingFinished.connect(self._pad_number)

        self.name = QLineEdit()
        self.start = QDateTimeEdit()
        self.end = QDateTimeEdit()
        for w in (self.start, self.end):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.goal = QLineEdit()
        self.board = QLabel()

        form = QFormLayout()
        form.addRow("Sprint config", cfg_row)
        form.addRow("Team", self.team)
        form.addRow("Project", self.project)
        form.addRow("Sprint number", self.number)
        form.addRow("Sprint name", self.name)
        form.addRow("Start", self.start)
        form.addRow("End", self.end)
        form.addRow("Goal", self.goal)
        form.addRow("Board", self.board)

        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.create_btn = QPushButton("Create Sprint")
        self.create_btn.clicked.connect(self._check)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.create_btn)
        buttons.addWidget(close)

        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.status)
        box.addStretch()
        box.addLayout(buttons)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.load_config()

    # ---------------------------------------------------------------- config ---

    def _say(self, text: str, error: bool = False):
        self.status.setStyleSheet("color: #c0392b;" if error else "")
        self.status.setText(text)

    def load_config(self):
        path = Path(self.config_path.text().strip())
        self.team.blockSignals(True)
        self.team.clear()
        self.teams = []
        try:
            self.teams, problems = load_teams(path)
        except SprintConfigError as exc:
            self.team.blockSignals(False)
            self._team_changed()
            self._say(f"{exc}\nUse 'New from template...' to create one.", error=True)
            return
        self.settings.setValue("sprint_config", str(path))
        for name in dict.fromkeys(t.team for t in self.teams):  # unique, in file order
            self.team.addItem(name)
        last = self.settings.value("sprint_team", "")
        if last and self.team.findText(last) >= 0:
            self.team.setCurrentIndex(self.team.findText(last))
        self.team.blockSignals(False)
        self._team_changed()
        if not self.teams:
            self._say("No teams in the sprint config." + _join(problems), error=True)
        elif problems:
            self._say("Some rows were skipped:" + _join(problems), error=True)
        else:
            self._say(f"{len(self.teams)} team/project rows loaded from {path.name}.")

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Sprint config", self.config_path.text(),
                                              "Excel (*.xlsx)")
        if path:
            self.config_path.setText(path)
            self.load_config()

    def _new_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save sprint config template",
                                              self.config_path.text(), "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_template(Path(path))
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.config_path.setText(path)
        self.load_config()
        self._say(f"Example config saved to {path}. Fill in your teams in Excel, then "
                  "choose it here again.")

    # ------------------------------------------------------------------ form ---

    def _team_changed(self, *_):
        team = self.team.currentText()
        self.project.blockSignals(True)
        self.project.clear()
        for t in self.teams:
            if t.team == team:
                self.project.addItem(t.project_key)
        last = self.settings.value("sprint_project", "")
        if team == self.settings.value("sprint_team", "") and self.project.findText(last) >= 0:
            self.project.setCurrentIndex(self.project.findText(last))
        self.project.blockSignals(False)
        self._project_changed()

    def _project_changed(self, *_):
        """Show the next sprint for this team and project: the one after the last sprint
        created here, or the config's first sprint number."""
        cfg = self.selected()
        if cfg:
            saved = self._next_numbers().get(_key(cfg))
            try:
                label = sprint_label(saved if saved is not None else cfg.first_number)
            except SprintConfigError:
                label = sprint_label(cfg.first_number)
            self.number.blockSignals(True)
            self.number.setText(label)
            self.number.blockSignals(False)
        self._refresh_plan()

    def _next_numbers(self) -> dict:
        try:
            data = json.loads(self.settings.value("sprint_next_numbers", "{}") or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _pad_number(self):
        """Show the number as it will be used: 1 -> 01."""
        try:
            label = sprint_label(self.number.text())
        except SprintConfigError:
            return
        if label != self.number.text():
            self.number.setText(label)

    def _remember_next(self, cfg: TeamConfig, label: str):
        data = self._next_numbers()
        data[_key(cfg)] = label
        self.settings.setValue("sprint_next_numbers", json.dumps(data))

    def selected(self) -> TeamConfig | None:
        team, project = self.team.currentText(), self.project.currentText()
        return next((t for t in self.teams if t.team == team and t.project_key == project), None)

    def _refresh_plan(self, *_):
        cfg = self.selected()
        self._plan_ok = False
        plan = None
        if cfg is not None:
            try:
                plan = plan_sprint(cfg, self.number.text())
            except SprintConfigError as exc:
                self._say(str(exc), error=True)
        if plan is None:
            # Don't leave a previous sprint's details in the form.
            for w in (self.name, self.goal, self.board):
                w.clear()
            self._refresh_enabled()
            return
        self._plan_ok = True
        self._refresh_enabled()
        self.name.setText(plan.name)
        self.start.setDateTime(QDateTime(plan.start))
        self.end.setDateTime(QDateTime(plan.end))
        self.goal.setText(plan.goal)
        self.board.setText(f"{plan.board_id} (from the sprint config)")

    def current_plan(self) -> SprintPlan | None:
        """The sprint as shown in the form, including any edits."""
        cfg = self.selected()
        if cfg is None:
            return None
        start = self.start.dateTime().toPython()
        end = self.end.dateTime().toPython()
        return SprintPlan(self.name.text().strip(), cfg.board_id, start, end,
                          self.goal.text().strip())

    # ---------------------------------------------------------------- create ---

    def _check(self):
        plan, cfg = self.current_plan(), self.selected()
        if plan is None or self._worker is not None or not self._plan_ok:
            return
        if not plan.name:
            self._say("Enter a sprint name.", error=True)
            return
        if len(plan.name) > MAX_NAME:
            self._say(f"Jira allows sprint names of up to {MAX_NAME} characters; "
                      f"'{plan.name}' has {len(plan.name)}. Shorten it here or change "
                      "the Sprint Name Format in the config.", error=True)
            return
        if plan.end <= plan.start:
            self._say("The end must be after the start.", error=True)
            return
        self.settings.setValue("sprint_team", cfg.team)
        self.settings.setValue("sprint_project", cfg.project_key)
        self._pending = (plan, cfg)
        self._say(f"Checking board {plan.board_id}...")
        self._run("check", plan, cfg.project_key)

    def _run(self, mode: str, plan: SprintPlan, project_key: str):
        _prune()
        worker = SprintWorker(self.client, mode, plan, project_key)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        thread = threading.Thread(target=worker.run, daemon=True, name=f"sprint-{mode}")
        # Workers are kept until a later _run on the GUI thread finds them done, so a
        # QObject is never freed on its worker thread.
        _WORKERS.add((thread, worker))
        self._worker = worker
        self._refresh_enabled()
        thread.start()

    def _refresh_enabled(self):
        busy = self._worker is not None
        on = self.selected() is not None and not busy
        for w in (self.team, self.project, self.number, self.name, self.start, self.end,
                  self.goal, self.config_path, self.browse, self.template):
            w.setEnabled(on or (w in (self.team, self.config_path, self.browse, self.template)
                                and not busy))
        self.create_btn.setEnabled(on and self._plan_ok)

    def _done(self):
        self._worker = None
        self._refresh_enabled()

    def _on_failed(self, msg: str):
        if self.sender() is not self._worker:
            return
        self._done()
        self._say(f"Sprint not created: {msg}", error=True)

    def _on_finished(self, mode: str, result: dict):
        if self.sender() is not self._worker:
            return
        self._done()
        plan, cfg = self._pending
        if mode == "check":
            lines = ["Create this sprint in Jira?", "",
                     f"Name: {plan.name}",
                     f"Board: {result['board_name']} ({plan.board_id})",
                     f"Start: {plan.start:%Y-%m-%d %H:%M}",
                     f"End: {plan.end:%Y-%m-%d %H:%M}"]
            if plan.goal:
                lines.append(f"Goal: {plan.goal}")
            if result["warnings"]:
                lines += ["", "Note: " + " ".join(result["warnings"])]
            answer = QMessageBox.question(self, "Create Sprint", "\n".join(lines),
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self._say("Not created.")
                return
            self._say(f"Creating {plan.name}...")
            self._run("create", plan, cfg.project_key)
            return
        self.created = result
        label = next_label(self.number.text())
        self._remember_next(cfg, label)
        self._say(f"Created sprint '{result.get('name', plan.name)}' (id {result.get('id')}, "
                  f"{result.get('state', 'future')}) on board {plan.board_id}.")
        if len(label) <= 6:
            self.number.setText(label)  # ready for the next one
            self._say(self.status.text() + " The form now shows the next sprint.")

    def reject(self):
        self._worker = None  # a request in flight can't be stopped; its result is ignored
        super().reject()


_WORKERS: set[tuple[threading.Thread, SprintWorker]] = set()


def _prune():
    for entry in [e for e in _WORKERS if not e[0].is_alive()]:
        _WORKERS.discard(entry)


def _key(cfg: TeamConfig) -> str:
    return f"{cfg.team}\n{cfg.project_key}"


def _join(problems: list[str]) -> str:
    return "".join(f"\n- {p}" for p in problems[:8]) + (
        f"\n- ...and {len(problems) - 8} more" if len(problems) > 8 else "")
