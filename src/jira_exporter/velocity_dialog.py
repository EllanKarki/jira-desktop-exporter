"""'Velocity Chart' dialog: committed vs completed story points over a team's recent sprints."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCharts import (
    QAbstractBarSeries,
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QValueAxis,
)
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPainter
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

from jira_exporter.jira_client import JiraClient
from jira_exporter.report_dialog import _WORKERS, ReportWorker, _int, _join, _prune
from jira_exporter.sprint_config import SprintConfigError, TeamConfig, load_teams
from jira_exporter.sprint_dialog import default_sprint_config_path
from jira_exporter.sprint_report import SprintRow
from jira_exporter.velocity import COLUMNS, average_velocity, velocity_values, \
    write_velocity_xlsx

COMMITTED_COLOR = QColor("#9DB4CC")
COMPLETED_COLOR = QColor("#1F4E78")


def short_names(team: str, names: list[str]) -> list[str]:
    """Drop the team name from the front of sprint names when they all start with it
    ("Team Alpha Sprint 01" -> "Sprint 01"), so the chart's labels fit."""
    prefix = team.strip() + " "
    if team.strip() and names and all(n.startswith(prefix) and n[len(prefix):].strip()
                                      for n in names):
        return [n[len(prefix):].strip() for n in names]
    return names


class VelocityDialog(QDialog):
    def __init__(self, client: JiraClient, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Velocity Chart")
        self.resize(960, 720)
        self.client, self.settings = client, settings
        self.teams: list[TeamConfig] = []
        self.rows: list[SprintRow] = []
        self.rows_team = ""  # the team the shown rows are for
        self._pending_team = ""
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
        self.count.setValue(_int(settings.value("velocity_sprint_count", 7), 7))
        self.count.setSuffix(" closed sprints")
        self.count.setMaximumWidth(180)

        form = QFormLayout()
        form.addRow("Sprint config", cfg_row)
        form.addRow("Team", self.team)
        form.addRow("Last", self.count)

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

    def load_config(self):
        path = Path(self.config_path.text().strip())
        self.team.clear()
        self.teams = []
        try:
            self.teams, problems = load_teams(path)
        except SprintConfigError as exc:
            self._say(f"{exc}\nThe velocity chart uses the same config as Create Sprint "
                      "(Jira Actions > Create Sprint > New from template...).", error=True)
            self._refresh_enabled()
            return
        self.settings.setValue("sprint_config", str(path))
        for name in dict.fromkeys(t.team for t in self.teams):
            self.team.addItem(name)
        last = self.settings.value("velocity_team", "")
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
        team = self.team.currentText()
        return list(dict.fromkeys((t.team, t.board_id) for t in self.teams if t.team == team))

    def run_report(self):
        targets = self.targets()
        if self._worker is not None or not targets:
            return
        self.settings.setValue("velocity_team", self.team.currentText())
        self.settings.setValue("velocity_sprint_count", self.count.value())
        _prune()
        worker = ReportWorker(self.client, targets, self.count.value())
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        thread = threading.Thread(target=worker.run, daemon=True, name="velocity")
        _WORKERS.add((thread, worker))
        self._worker = worker
        self._pending_team = self.team.currentText()
        self._say("Starting...")
        self._refresh_enabled()
        thread.start()

    def _refresh_enabled(self):
        busy = self._worker is not None
        for w in (self.config_path, self.browse, self.team, self.count):
            w.setEnabled(not busy)
        self.run_btn.setEnabled(not busy and bool(self.teams))
        self.export_btn.setEnabled(not busy and bool(self.rows))
        self.image_btn.setEnabled(not busy and bool(self.rows))

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
        self.rows, self.rows_team = rows, self._pending_team
        self._fill()
        self._refresh_enabled()
        average = average_velocity(rows)
        if average is None:
            text = "No closed sprints to chart."
        else:
            text = (f"{len(rows)} sprint{'s' if len(rows) != 1 else ''}. Average velocity: "
                    f"{average:.1f} story points per sprint.")
        if field:
            text += f" Story points come from the board's estimate field ({field})."
        if warnings:
            text += "\nNot included:" + _join(warnings)
        self._say(text, error=not rows)

    def _fill(self):
        self.chart.removeAllSeries()
        for axis in self.chart.axes():
            self.chart.removeAxis(axis)
        self.chart.setTitle(f"{self.rows_team} velocity" if self.rows else "")
        committed, completed = QBarSet("Committed"), QBarSet("Completed")
        committed.setColor(COMMITTED_COLOR)
        completed.setColor(COMPLETED_COLOR)
        for bar_set in (committed, completed):
            bar_set.setLabelColor(QColor("#222222"))
        for row in self.rows:
            committed.append(row.committed_points)
            completed.append(row.completed_points)
        series = QBarSeries()
        series.append(committed)
        series.append(completed)
        series.setLabelsVisible(True)
        series.setLabelsPosition(QAbstractBarSeries.LabelsPosition.LabelsOutsideEnd)
        self.chart.addSeries(series)
        x = QBarCategoryAxis()
        x.append(short_names(self.rows_team, [r.sprint for r in self.rows]))
        if len(self.rows) > 8:
            x.setLabelsAngle(-30)  # long sprint names stay readable
        y = QValueAxis()
        top = max([r.committed_points for r in self.rows] +
                  [r.completed_points for r in self.rows] + [1])
        y.setRange(0, top * 1.15)  # room for the labels above the bars
        y.applyNiceNumbers()
        y.setLabelFormat("%g")
        y.setTitleText("Story points")
        self.chart.addAxis(x, Qt.AlignBottom)
        self.chart.addAxis(y, Qt.AlignLeft)
        series.attachAxis(x)
        series.attachAxis(y)

        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            for c, value in enumerate(velocity_values(row)):
                if c == len(COLUMNS) - 1:
                    value = "" if value is None else f"{value:.0%}"
                item = QTableWidgetItem(str(value))
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)

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
        if not self.rows:
            return
        path = self._ask_path("Save velocity report", "velocity.xlsx", "Excel (*.xlsx)",
                              ".xlsx")
        if path is None:
            return
        try:
            write_velocity_xlsx(path, self.rows_team, self.rows)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self._say(f"Saved to {path}.")

    def _save_image(self):
        if not self.rows:
            return
        path = self._ask_path("Save chart image", "velocity.png", "PNG image (*.png)", ".png")
        if path is None:
            return
        if not self.chart_view.grab().save(str(path)):
            QMessageBox.critical(self, "Could not save", f"Could not write {path}.")
            return
        self._say(f"Chart saved to {path}.")

    def reject(self):
        if self._worker is not None:
            self._worker.cancelled = True
        self._worker = None
        super().reject()
