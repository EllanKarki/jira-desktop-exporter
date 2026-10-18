"""'Sprint Report' dialog: per team and closed sprint, committed vs completed work."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from jira_exporter.jira_client import JiraClient, JiraError
from jira_exporter.sprint_config import SprintConfigError, TeamConfig, load_teams
from jira_exporter.sprint_dialog import default_sprint_config_path
from jira_exporter.sprint_report import (
    COLUMNS,
    SprintRow,
    estimate_field,
    recent_closed_sprints,
    summarize,
    write_report_xlsx,
)

ALL_TEAMS = "All teams"
# The table's headers on two lines, so the columns stay narrow; Excel gets COLUMNS.
TABLE_HEADERS = ["Team Name", "Sprint Name", "Committed\nIssues", "Committed\nStory Points",
                 "Completed\nIssues", "Completed\nStory Points", "Spill Over\nIssues",
                 "Spill Over\nStory Points", "Adjusted\nDuring Sprint",
                 "% Completed\nvs Committed"]


class ReportWorker(QObject):
    """Fetch each board's recent closed sprints and their sprint reports.

    Runs on a daemon Python thread, like the Create Sprint worker, so a slow Jira
    request can't hold up the app when it quits."""

    progress = Signal(str)
    finished = Signal(object, object, str)  # rows, warnings, estimate field name
    failed = Signal(str)

    def __init__(self, client: JiraClient, targets: list[tuple[str, int]], count: int):
        super().__init__()
        self.client, self.targets, self.count = client, targets, count
        self.cancelled = False

    def run(self):
        try:
            self.finished.emit(*self._run())
        except JiraError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep the dialog alive on anything unexpected
            self.failed.emit(f"Unexpected error: {exc}")

    def _run(self):
        rows: list[SprintRow] = []
        warnings: list[str] = []
        field = None
        for team, board_id in self.targets:
            if self.cancelled:
                break
            self.progress.emit(f"{team}: reading sprints on board {board_id}...")
            try:
                sprints = recent_closed_sprints(self.client.board_sprints(board_id),
                                                board_id, self.count)
            except JiraError as exc:
                warnings.append(f"{team}, board {board_id}: {exc}")
                continue
            if not sprints:
                warnings.append(f"{team}, board {board_id}: no closed sprints.")
            for sprint in sprints:
                if self.cancelled:
                    break
                self.progress.emit(f"{team}: {sprint.get('name')}...")
                try:
                    report = self.client.sprint_report(board_id, sprint["id"])
                except JiraError as exc:
                    warnings.append(f"{team}, {sprint.get('name')}: {exc}")
                    continue
                field = field or estimate_field(report)
                rows.append(summarize(team, sprint, report, board_id))
        return rows, warnings, self._field_name(field)

    def _field_name(self, field: str | None) -> str:
        if not field:
            return ""
        try:
            names = {f.get("id"): f.get("name") for f in self.client.fields()}
        except JiraError:
            names = {}
        return names.get(field) or field


class SprintReportDialog(QDialog):
    def __init__(self, client: JiraClient, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sprint Report")
        self.resize(1180, 520)
        self.client, self.settings = client, settings
        self.teams: list[TeamConfig] = []
        self.rows: list[SprintRow] = []
        self._worker: ReportWorker | None = None

        self.config_path = QLineEdit(settings.value("sprint_config",
                                                    str(default_sprint_config_path())))
        self.config_path.editingFinished.connect(self.load_config)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.config_path, 1)
        cfg_row.addWidget(self.browse)

        self.team = QComboBox()
        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(_int(settings.value("report_sprint_count", 6), 6))
        self.count.setSuffix(" closed sprints")
        self.count.setMaximumWidth(180)
        self.count.setToolTip("How many of each board's most recent closed sprints to report")

        form = QFormLayout()
        form.addRow("Sprint config", cfg_row)
        form.addRow("Team", self.team)
        form.addRow("Last", self.count)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        for c, full in enumerate(COLUMNS):
            self.table.horizontalHeaderItem(c).setToolTip(full)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)

        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.run_btn = QPushButton("Run report")
        self.run_btn.clicked.connect(self.run_report)
        self.export_btn = QPushButton("Export to Excel...")
        self.export_btn.clicked.connect(self._export)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.export_btn)
        buttons.addWidget(close)

        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.table, 1)
        box.addWidget(self.status)
        box.addLayout(buttons)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.load_config()

    def _say(self, text: str, error: bool = False):
        self.status.setStyleSheet("color: #c0392b;" if error else "")
        self.status.setText(text)

    def load_config(self):
        path = Path(self.config_path.text().strip())
        self.team.clear()
        self.teams = []
        try:
            self.teams, problems = load_teams(path)
        except SprintConfigError as exc:
            self._say(f"{exc}\nThe sprint report uses the same config as Create Sprint "
                      "(Jira Actions > Create Sprint > New from template...).", error=True)
            self._refresh_enabled()
            return
        self.settings.setValue("sprint_config", str(path))
        if len(self.teams) > 1:
            self.team.addItem(ALL_TEAMS)
        for name in dict.fromkeys(t.team for t in self.teams):
            self.team.addItem(name)
        last = self.settings.value("report_team", "")
        if last and self.team.findText(last) >= 0:
            self.team.setCurrentIndex(self.team.findText(last))
        if not self.teams:
            self._say("No teams in the sprint config." + _join(problems), error=True)
        elif problems:
            self._say("Some rows were skipped:" + _join(problems), error=True)
        else:
            self._say(f"{len(self.teams)} team/project rows loaded from {path.name}.")
        self._refresh_enabled()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Sprint config", self.config_path.text(),
                                              "Excel (*.xlsx)")
        if path:
            self.config_path.setText(path)
            self.load_config()

    def targets(self) -> list[tuple[str, int]]:
        """(team, board) pairs to report, in config order. A board shared by several
        project rows of one team is reported once."""
        team = self.team.currentText()
        pairs = [(t.team, t.board_id) for t in self.teams if team in (ALL_TEAMS, t.team)]
        return list(dict.fromkeys(pairs))

    def run_report(self):
        targets = self.targets()
        if self._worker is not None or not targets:
            return
        self.settings.setValue("report_team", self.team.currentText())
        self.settings.setValue("report_sprint_count", self.count.value())
        _prune()
        worker = ReportWorker(self.client, targets, self.count.value())
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        thread = threading.Thread(target=worker.run, daemon=True, name="sprint-report")
        _WORKERS.add((thread, worker))
        self._worker = worker
        self._say("Starting...")
        self._refresh_enabled()
        thread.start()

    def _refresh_enabled(self):
        busy = self._worker is not None
        for w in (self.config_path, self.browse, self.team, self.count):
            w.setEnabled(not busy)
        self.run_btn.setEnabled(not busy and bool(self.teams))
        self.export_btn.setEnabled(not busy and bool(self.rows))

    def _on_progress(self, text: str):
        if self.sender() is self._worker:
            self._say(text)

    def _on_failed(self, msg: str):
        if self.sender() is not self._worker:
            return
        self._worker = None
        self._refresh_enabled()
        self._say(f"Report failed: {msg}", error=True)

    def _on_finished(self, rows: list[SprintRow], warnings: list[str], field: str):
        if self.sender() is not self._worker:
            return
        self._worker = None
        self.rows = rows
        self._fill_table()
        self._refresh_enabled()
        text = f"{len(rows)} sprint{'s' if len(rows) != 1 else ''} reported."
        if field:
            text += f" Story points come from each board's estimate field ({field})."
        if warnings:
            text += "\nNot included:" + _join(warnings)
        self._say(text, error=bool(warnings) and not rows)

    def _fill_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            for c, value in enumerate(row.values()):
                item = _Item()
                if c == len(COLUMNS) - 1:
                    item.setData(Qt.DisplayRole, "" if value is None else f"{value:.0%}")
                    item.setData(Qt.UserRole, -1 if value is None else value)
                elif isinstance(value, (int, float)):
                    item.setData(Qt.DisplayRole, value)  # sorts as a number
                else:
                    item.setData(Qt.DisplayRole, str(value))
                if c >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)

    def _export(self):
        if not self.rows:
            return
        start = self.settings.value("report_dir", str(Path.home()))
        path, _ = QFileDialog.getSaveFileName(self, "Save sprint report",
                                              str(Path(start) / "sprint-report.xlsx"),
                                              "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_report_xlsx(Path(path), self.rows)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.settings.setValue("report_dir", str(Path(path).parent))
        self._say(f"Saved {len(self.rows)} rows to {path}.")

    def reject(self):
        if self._worker is not None:
            self._worker.cancelled = True  # stops after the request in flight
        self._worker = None
        super().reject()


class _Item(QTableWidgetItem):
    """Sorts numbers as numbers, and the percentage column by its value (kept in
    UserRole) rather than its text."""

    def __lt__(self, other):
        mine, theirs = self.data(Qt.UserRole), other.data(Qt.UserRole)
        if mine is None or theirs is None:
            mine, theirs = self.data(Qt.DisplayRole), other.data(Qt.DisplayRole)
        try:
            return mine < theirs
        except TypeError:
            return str(mine) < str(theirs)


_WORKERS: set[tuple[threading.Thread, ReportWorker]] = set()


def _prune():
    for entry in [e for e in _WORKERS if not e[0].is_alive()]:
        _WORKERS.discard(entry)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _join(lines: list[str]) -> str:
    return "".join(f"\n- {p}" for p in lines[:8]) + (
        f"\n- ...and {len(lines) - 8} more" if len(lines) > 8 else "")
