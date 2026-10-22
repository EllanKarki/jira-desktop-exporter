"""'Discover fields' dialog: scan a project's fields and save a fields.json from them."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
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

from jira_exporter.field_discovery import (
    DiscoveryResult,
    FieldUsage,
    discover_fields,
    discover_from_issue,
    write_config,
    write_field_list_xlsx,
)
from jira_exporter.jira_client import JiraClient, JiraError

# Scans still running after their dialog closed. They finish in the background (a
# request in flight can't be interrupted) instead of freezing the window while we wait.
_RUNNING: set[tuple[QThread, QObject]] = set()


def _wait_for_scans():
    for thread, worker in list(_RUNNING):
        worker.cancel()
        thread.quit()
        thread.wait(5000)


COLUMNS = ["Include", "Field name", "Field id", "Type", "Issues with a value", "On create screen for"]


class DiscoverWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # DiscoveryResult
    failed = Signal(str)

    def __init__(self, client: JiraClient, project_key: str, sample_size: int,
                 one_issue: bool = False):
        super().__init__()
        self.client, self.project_key, self.sample_size = client, project_key, sample_size
        self.one_issue = one_issue
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @Slot()
    def run(self):
        self.client.notify_waits(lambda s: self.progress.emit(
            f"Jira asked the app to slow down; waiting {s:.0f} s before trying again..."))
        try:
            if self.one_issue:
                result = discover_from_issue(self.client, self.project_key,
                                             progress=self.progress.emit,
                                             cancelled=lambda: self._cancel)
            else:
                result = discover_fields(self.client, self.project_key, self.sample_size,
                                         progress=self.progress.emit,
                                         cancelled=lambda: self._cancel)
            self.finished.emit(result)
        except JiraError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep the dialog alive on anything unexpected
            self.failed.emit(f"Unexpected error: {exc}")


class FieldsDialog(QDialog):
    """Lists the fields a project uses; the ticked ones can be saved as fields.json."""

    config_saved = Signal(str)

    def __init__(self, client: JiraClient, settings: QSettings, config_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Discover project fields")
        self.resize(900, 600)
        self.client, self.settings = client, settings
        self.template = Path(config_path) if config_path else None
        self.result: DiscoveryResult | None = None
        self._rows: list[FieldUsage] = []
        self._thread: QThread | None = None
        self._worker: DiscoverWorker | None = None
        self._checked: set[str] = set()  # ticked field ids, shown or not

        self.project = QLineEdit(settings.value("discover_project", ""))
        self.project.setPlaceholderText("Project key, e.g. ABC")
        self.project.returnPressed.connect(self._scan)
        self.sample = QSpinBox()
        self.sample.setRange(0, 5000)
        self.sample.setSingleStep(100)
        self.sample.setValue(int(settings.value("discover_sample", 200)))
        self.sample.setToolTip("How many of the most recently updated issues to check for "
                               "filled-in fields (0 = only read the create screens).")
        self.mode = QComboBox()
        self.mode.addItem("Full scan", False)
        self.mode.addItem("One sample issue", True)
        self.mode.setToolTip("Full scan: the project's create screens plus recent issues.\n"
                             "One sample issue: read a single issue's fields (one or two "
                             "requests), e.g. when Jira limits how many requests you can make.")
        self.mode.setCurrentIndex(1 if settings.value("discover_one_issue", False,
                                                      type=bool) else 0)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._scan)
        self.sample_label = QLabel("Issues to check")
        top = QHBoxLayout()
        top.addWidget(QLabel("Project"))
        top.addWidget(self.project, 1)
        top.addWidget(self.mode)
        top.addWidget(self.sample_label)
        top.addWidget(self.sample)
        top.addWidget(self.scan_btn)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or id")
        self.filter.textChanged.connect(self._apply_filter)
        self.show_all = QCheckBox("Show fields the project doesn't use")
        self.show_all.toggled.connect(self._fill_table)
        filt = QHBoxLayout()
        filt.addWidget(self.filter, 1)
        filt.addWidget(self.show_all)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Stretch)

        self.table.itemChanged.connect(self._item_changed)

        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self._say("Enter a project key and click Scan.")

        self.select_used_btn = QPushButton("Tick used fields")
        self.select_used_btn.clicked.connect(lambda: self._tick(lambda f: f.suggested))
        self.tick_all_btn = QPushButton("Tick all")
        self.tick_all_btn.setToolTip("Tick every field listed (all of them when the filter is empty)")
        self.tick_all_btn.clicked.connect(self._tick_all_shown)
        self.clear_btn = QPushButton("Untick all")
        self.clear_btn.clicked.connect(lambda: self._tick(lambda f: False))
        self.save_btn = QPushButton("Save as fields.json...")
        self.save_btn.clicked.connect(self._save_config)
        self.xlsx_btn = QPushButton("Export field list to Excel...")
        self.xlsx_btn.clicked.connect(self._save_xlsx)
        bottom = QHBoxLayout()
        bottom.addWidget(self.select_used_btn)
        bottom.addWidget(self.tick_all_btn)
        bottom.addWidget(self.clear_btn)
        bottom.addStretch()
        bottom.addWidget(self.xlsx_btn)
        bottom.addWidget(self.save_btn)
        self._set_result_buttons(False)

        box = QVBoxLayout(self)
        box.addLayout(top)
        box.addLayout(filt)
        box.addWidget(self.table, 1)
        box.addWidget(self.status)
        box.addLayout(bottom)
        # Enter in the filter or the table must not "click" a button (e.g. rescan and
        # reset the ticks); Enter in the project box still scans.
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)
        self._mode_changed()

    # ------------------------------------------------------------- scanning ---

    def _say(self, text: str, error: bool = False):
        self.status.setStyleSheet("color: #c0392b;" if error else "")
        self.status.setText(text)

    def _one_issue(self) -> bool:
        return bool(self.mode.currentData())

    def _mode_changed(self, *_):
        one = self._one_issue()
        self.sample_label.setVisible(not one)
        self.sample.setVisible(not one)
        self.project.setPlaceholderText("Issue key (ABC-123), or a project key to use its "
                                        "latest issue" if one else "Project key, e.g. ABC")

    def _set_result_buttons(self, on: bool):
        for b in (self.select_used_btn, self.tick_all_btn, self.clear_btn, self.save_btn,
                  self.xlsx_btn):
            b.setEnabled(on)

    def _scan(self):
        key = self.project.text().strip()
        if not key:
            self._say("Enter an issue or project key first." if self._one_issue()
                      else "Enter a project key first.")
            return
        if self._thread is not None:
            return
        self.settings.setValue("discover_project", key)
        self.settings.setValue("discover_sample", self.sample.value())
        self.settings.setValue("discover_one_issue", self._one_issue())
        self.scan_btn.setEnabled(False)
        self._set_result_buttons(False)
        self._say(f"Scanning {key}...")
        worker = DiscoverWorker(self.client, key, self.sample.value(), self._one_issue())
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        entry = (thread, worker)
        _RUNNING.add(entry)
        thread.finished.connect(lambda: _RUNNING.discard(entry))
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        app = QApplication.instance()
        if app is not None and not getattr(app, "_scan_cleanup_hooked", False):
            app.aboutToQuit.connect(_wait_for_scans)
            app._scan_cleanup_hooked = True
        self._thread, self._worker = thread, worker
        thread.start()

    def _current(self) -> bool:
        """True when a signal comes from the scan this dialog is still waiting for."""
        return self._worker is not None and self.sender() is self._worker

    def _done(self):
        self._thread = self._worker = None
        self.scan_btn.setEnabled(True)

    def _on_progress(self, msg: str):
        if self._current():
            self._say(msg)

    def _on_failed(self, msg: str):
        if not self._current():
            return
        self._done()
        if self.result is not None:
            msg += (f"\nThe table still shows the previous scan of {self.result.project_key}.")
            self._set_result_buttons(True)
        self._say(f"Scan failed: {msg}", error=True)

    def _on_result(self, result: DiscoveryResult):
        if not self._current():
            return
        self._done()
        self.result = result
        used = sum(1 for f in result.fields if f.used_in and not f.is_key)
        on_screens = sum(1 for f in result.fields if f.issue_types)
        if result.source_issue:
            empty = sum(1 for f in result.fields if not f.used_in)
            msg = (f"{result.project_name} ({result.project_key}): {used} fields have values "
                   f"on {result.source_issue}, and {empty} more are empty on it (tick 'Show "
                   "fields the project doesn't use' to list them). Ticked: fields with values.")
        else:
            msg = (f"{result.project_name} ({result.project_key}): {len(result.relevant)} "
                   f"fields in use. {used} have values in the {result.sampled} most recently "
                   f"updated issues, {on_screens} are on a create screen. Ticked: fields with "
                   "values.")
        if result.warnings:
            msg += " Note: " + " ".join(result.warnings)
        self._say(msg)
        self._checked = {f.id for f in result.fields if f.suggested}
        self._fill_table()
        self._set_result_buttons(True)

    # ---------------------------------------------------------------- table ---

    def _item_changed(self, item: QTableWidgetItem):
        if item.column() != 0 or not 0 <= item.row() < len(self._rows):
            return
        fid = self._rows[item.row()].id
        if item.checkState() == Qt.Checked:
            self._checked.add(fid)
        else:
            self._checked.discard(fid)

    def _fill_table(self, *_):
        if not self.result:
            return
        self.table.blockSignals(True)
        self._rows = self.result.fields if self.show_all.isChecked() else self.result.relevant
        self.table.setRowCount(len(self._rows))
        sampled = self.result.sampled
        for row, f in enumerate(self._rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if f.id in self._checked else Qt.Unchecked)
            used = QTableWidgetItem(f"{f.used_in} of {sampled}" if sampled else "-")
            used.setData(Qt.UserRole, f.used_in)
            cells = [check, QTableWidgetItem(f.name), QTableWidgetItem("key" if f.is_key else f.id),
                     QTableWidgetItem(f.type), used,
                     QTableWidgetItem(", ".join(sorted(f.issue_types)))]
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        for col in (0, 2, 3, 4):
            self.table.resizeColumnToContents(col)
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str):
        needle = text.strip().lower()
        for row, f in enumerate(self._rows):
            hide = bool(needle) and needle not in f.name.lower() and needle not in f.id.lower()
            self.table.setRowHidden(row, hide)

    def _tick(self, rule):
        if self.result:
            self._checked = {f.id for f in self.result.fields if rule(f)}
            self._fill_table()

    def _tick_all_shown(self):
        """Tick every row currently listed; rows hidden by the filter keep their state."""
        if self.result:
            self._checked |= {f.id for row, f in enumerate(self._rows)
                              if not self.table.isRowHidden(row)}
            self._fill_table()

    def selected_fields(self) -> list[FieldUsage]:
        """Ticked fields, shown or hidden, in table order (key first, then most used)."""
        if not self.result:
            return []
        return [f for f in self.result.fields if f.id in self._checked]

    # --------------------------------------------------------------- saving ---

    def _save_config(self):
        chosen = self.selected_fields()
        if not chosen:
            QMessageBox.warning(self, "Nothing ticked", "Tick at least one field to export.")
            return
        start = str(self.template) if self.template else "fields.json"
        path, _ = QFileDialog.getSaveFileName(self, "Save fields config", start, "JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            write_config(Path(path), chosen, template=self.template)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.config_saved.emit(path)
        self._say(f"Saved {len(chosen)} fields to {path}. The export screen now uses this file.")

    def _save_xlsx(self):
        if not self.result:
            return
        start = str(Path(self.settings.value("out_dir", str(Path.home())))
                    / f"{self.result.project_key}_fields.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Save field list", start, "Excel (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            write_field_list_xlsx(Path(path), self.result)
        except PermissionError:
            QMessageBox.critical(self, "Could not save", f"Cannot write {path}. Is it open in Excel?")
            return
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self._say(f"Field list saved to {path}.")

    def reject(self):
        if self._worker is not None:
            # Let the scan wind down in the background; its result is ignored.
            self._worker.cancel()
            self._done()
        super().reject()
