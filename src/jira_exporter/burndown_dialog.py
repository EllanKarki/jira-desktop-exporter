"""'Burndown Chart' dialog: one sprint's remaining story points, day by day."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from jira_exporter.burndown import COLUMNS, Burndown, BurndownError, build_burndown, \
    write_burndown_xlsx
from jira_exporter.jira_client import JiraClient, JiraError
from jira_exporter.report_dialog import _join
from jira_exporter.sprint_config import SprintConfigError, TeamConfig, load_teams
from jira_exporter.sprint_dialog import default_sprint_config_path

REMAINING_COLOR = QColor("#C0392B")
GUIDELINE_COLOR = QColor("#7F8C8D")


def burndown_sprints(sprints: list[dict], board_id: int) -> list[dict]:
    """The board's own active and closed sprints, newest first."""
    own = [s for s in sprints if (s.get("state") or "").lower() in ("active", "closed")
           and s.get("originBoardId") in (None, board_id)]
    own.sort(key=lambda s: ((s.get("state") or "").lower() == "active",
                            s.get("completeDate") or s.get("startDate") or "",
                            s.get("id") or 0), reverse=True)
    return own


class Worker(QObject):
    """Runs one Jira job on a daemon thread, like the other report workers."""

    finished = Signal(str, object)  # mode, result
    failed = Signal(str)

    def __init__(self, mode: str, job: Callable[[], object]):
        super().__init__()
        self.mode, self.job = mode, job

    def run(self):
        try:
            self.finished.emit(self.mode, self.job())
        except (JiraError, BurndownError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep the dialog alive on anything unexpected
            self.failed.emit(f"Unexpected error: {exc}")


class BurndownDialog(QDialog):
    def __init__(self, client: JiraClient, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Burndown Chart")
        self.resize(960, 720)
        self.client, self.settings = client, settings
        self.teams: list[TeamConfig] = []
        self.sprints: list[tuple[int, dict]] = []  # (board, sprint) in the Sprint list
        self.chart_data: Burndown | None = None
        self.chart_title = ""
        self._worker: Worker | None = None

        self.config_path = QLineEdit(settings.value("sprint_config",
                                                    str(default_sprint_config_path())))
        self.config_path.editingFinished.connect(self.load_config)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.config_path, 1)
        cfg_row.addWidget(self.browse)

        self.team = QComboBox()
        self.team.currentIndexChanged.connect(self._load_sprints)
        self.sprint = QComboBox()
        self.sprint.setMinimumWidth(320)

        form = QFormLayout()
        form.addRow("Sprint config", cfg_row)
        form.addRow("Team", self.team)
        form.addRow("Sprint", self.sprint)

        self.chart = QChart()
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(300)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
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
        self.image_btn = QPushButton("Save chart image...")
        self.image_btn.clicked.connect(self._save_image)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        for b in (self.run_btn, self.export_btn, self.image_btn, close):
            buttons.addWidget(b)

        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.chart_view, 3)
        box.addWidget(self.table, 2)
        box.addWidget(self.status)
        box.addLayout(buttons)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.load_config()

    def _say(self, text: str, error: bool = False):
        self.status.setStyleSheet("color: #c0392b;" if error else "")
        self.status.setText(text)

    # ---------------------------------------------------------------- config ---

    def load_config(self):
        path = Path(self.config_path.text().strip())
        self.team.blockSignals(True)
        self.team.clear()
        self.teams = []
        try:
            self.teams, problems = load_teams(path)
        except SprintConfigError as exc:
            self.team.blockSignals(False)
            self._say(f"{exc}\nThe burndown chart uses the same config as Create Sprint "
                      "(Jira Actions > Create Sprint > New from template...).", error=True)
            self._refresh_enabled()
            return
        self.settings.setValue("sprint_config", str(path))
        for name in dict.fromkeys(t.team for t in self.teams):
            self.team.addItem(name)
        last = self.settings.value("burndown_team", "")
        if last and self.team.findText(last) >= 0:
            self.team.setCurrentIndex(self.team.findText(last))
        self.team.blockSignals(False)
        if not self.teams:
            self._say("No teams in the sprint config." + _join(problems), error=True)
        elif problems:
            self._say("Some rows were skipped:" + _join(problems), error=True)
        self._load_sprints()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Sprint config", self.config_path.text(),
                                              "Excel (*.xlsx)")
        if path:
            self.config_path.setText(path)
            self.load_config()

    def boards(self) -> list[int]:
        team = self.team.currentText()
        return list(dict.fromkeys(t.board_id for t in self.teams if t.team == team))

    # --------------------------------------------------------------- workers ---

    def _start(self, mode: str, job: Callable[[], object]):
        _prune()
        worker = Worker(mode, job)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        thread = threading.Thread(target=worker.run, daemon=True, name=f"burndown-{mode}")
        _WORKERS.add((thread, worker))
        self._worker = worker
        self._refresh_enabled()
        thread.start()

    def _load_sprints(self, *_):
        self.sprint.clear()
        self.sprints = []
        boards = self.boards()
        if not boards:
            self._refresh_enabled()
            return

        def job():
            found, problems = [], []
            for board in boards:
                try:
                    found += [(board, s) for s in
                              burndown_sprints(self.client.board_sprints(board), board)]
                except JiraError as exc:
                    problems.append(f"Board {board}: {exc}")
            return found, problems

        self._say(f"Reading {self.team.currentText()}'s sprints...")
        self._start("sprints", job)

    def run_report(self):
        i = self.sprint.currentIndex()
        if self._worker is not None or not 0 <= i < len(self.sprints):
            return
        board, sprint = self.sprints[i]
        self.settings.setValue("burndown_team", self.team.currentText())
        self._pending_title = sprint.get("name") or str(sprint.get("id"))
        self._say(f"Reading {self._pending_title}...")
        self._start("burndown", lambda: build_burndown(
            self.client.scope_change_burndown(board, sprint["id"])))

    def _refresh_enabled(self):
        busy = self._worker is not None
        for w in (self.config_path, self.browse, self.team, self.sprint):
            w.setEnabled(not busy)
        self.run_btn.setEnabled(not busy and bool(self.sprints))
        self.export_btn.setEnabled(not busy and self.chart_data is not None)
        self.image_btn.setEnabled(not busy and self.chart_data is not None)

    def _on_failed(self, msg: str):
        if self.sender() is not self._worker:
            return
        self._worker = None
        self._refresh_enabled()
        self._say(f"Report failed: {msg}", error=True)

    def _on_finished(self, mode: str, result):
        if self.sender() is not self._worker:
            return
        self._worker = None
        if mode == "sprints":
            self.sprints, problems = result
            for _, s in self.sprints:
                label = s.get("name") or str(s.get("id"))
                self.sprint.addItem(label + (" (active)" if s.get("state") == "active" else ""))
            if self.sprints:
                text = f"{len(self.sprints)} sprints found. Pick one and click Run report."
            else:
                text = "No active or closed sprints on this team's board."
            if problems:
                text += "\nNot included:" + _join(problems)
            self._say(text, error=not self.sprints)
        else:
            self.chart_data, self.chart_title = result, self._pending_title
            self._fill()
            c = result
            when = "when it closed" if c.completed else "now (the sprint is still active)"
            self._say(f"{self.chart_title}: {tidy(c.start_remaining)} story points at the "
                      f"start, {tidy(c.remaining)} remaining {when}.")
        self._refresh_enabled()

    # ----------------------------------------------------------------- chart ---

    def _fill(self):
        c = self.chart_data
        self.chart.removeAllSeries()
        for axis in self.chart.axes():
            self.chart.removeAxis(axis)
        self.chart.setTitle(f"{self.chart_title} burndown")

        remaining = QLineSeries()
        remaining.setName("Remaining")
        remaining.setPen(QPen(REMAINING_COLOR, 2.5))
        previous = None
        for when, value in c.steps:
            if previous is not None:  # a step: hold the old value until the change
                remaining.append(_ms(when), previous)
            remaining.append(_ms(when), value)
            previous = value
        remaining.append(_ms(c.stop), previous)

        guideline = QLineSeries()
        guideline.setName("Guideline")
        pen = QPen(GUIDELINE_COLOR, 1.5)
        pen.setStyle(Qt.DashLine)
        guideline.setPen(pen)
        guideline.append(_ms(c.start), c.start_remaining)
        guideline.append(_ms(c.end), 0)

        x = QDateTimeAxis()
        x.setFormat("MMM d")
        last = max(c.end, c.stop)
        x.setRange(QDateTime(c.start), QDateTime(last))
        days = round((last - c.start).total_seconds() / 86400)
        # One tick a day for sprints up to three weeks, so the labels land on the same
        # time each day; longer ones get fewer.
        x.setTickCount(days + 1 if 1 <= days <= 21 else 12)
        y = QValueAxis()
        top = max([v for _, v in c.steps] + [c.start_remaining, 1])
        y.setRange(0, top * 1.1)
        y.applyNiceNumbers()
        y.setLabelFormat("%g")
        y.setTitleText("Story points")
        self.chart.addAxis(x, Qt.AlignBottom)
        self.chart.addAxis(y, Qt.AlignLeft)
        for series in (guideline, remaining):
            self.chart.addSeries(series)
            series.attachAxis(x)
            series.attachAxis(y)

        self.table.setRowCount(len(c.days))
        for r, day in enumerate(c.days):
            for col, value in enumerate(day.values()):
                text = value.isoformat() if col == 0 else str(value)
                item = QTableWidgetItem(text)
                if col:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, col, item)

    # ---------------------------------------------------------------- export ---

    def _ask_path(self, title: str, name: str, filt: str, ext: str) -> Path | None:
        start = self.settings.value("report_dir", str(Path.home()))
        path, _ = QFileDialog.getSaveFileName(self, title, str(Path(start) / name), filt)
        if not path:
            return None
        if not path.lower().endswith(ext):
            path += ext
        self.settings.setValue("report_dir", str(Path(path).parent))
        return Path(path)

    def _export(self):
        if self.chart_data is None:
            return
        path = self._ask_path("Save burndown", "burndown.xlsx", "Excel (*.xlsx)", ".xlsx")
        if path is None:
            return
        try:
            write_burndown_xlsx(path, self.chart_title, self.chart_data)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self._say(f"Saved to {path}.")

    def _save_image(self):
        if self.chart_data is None:
            return
        path = self._ask_path("Save chart image", "burndown.png", "PNG image (*.png)", ".png")
        if path is None:
            return
        if not self.chart_view.grab().save(str(path)):
            QMessageBox.critical(self, "Could not save", f"Could not write {path}.")
            return
        self._say(f"Chart saved to {path}.")

    def reject(self):
        self._worker = None  # a request in flight can't be stopped; its result is ignored
        super().reject()


def _ms(when: datetime) -> float:
    return float(QDateTime(when).toMSecsSinceEpoch())


def tidy(points: float) -> str:
    return f"{points:g}"


_WORKERS: set[tuple[threading.Thread, Worker]] = set()


def _prune():
    for entry in [e for e in _WORKERS if not e[0].is_alive()]:
        _WORKERS.discard(entry)
