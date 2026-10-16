"""PySide6 desktop UI: a login screen and an export screen."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jira_exporter import __version__
from jira_exporter.config import (
    ConfigError,
    ExportConfig,
    default_config_path,
    load_config,
    resolve_fields,
)
from jira_exporter.exporter import write_xlsx
from jira_exporter.jira_client import (
    CLOUD,
    DATA_CENTER,
    Credentials,
    JiraClient,
    JiraError,
    guess_deployment,
)


# ---------------------------------------------------------------- workers ---

class LoginWorker(QObject):
    succeeded = Signal(object, dict)  # client, user
    failed = Signal(str)

    def __init__(self, creds: Credentials):
        super().__init__()
        self.creds = creds

    @Slot()
    def run(self):
        try:
            client = JiraClient(self.creds)
            user = client.myself()
            self.succeeded.emit(client, user)
        except JiraError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep the UI alive on anything unexpected
            self.failed.emit(f"Unexpected error: {exc}")


class ExportWorker(QObject):
    progress = Signal(int, int)
    log = Signal(str)
    finished = Signal(int, str)  # count, path
    failed = Signal(str)

    def __init__(self, client: JiraClient, config: ExportConfig, jql: str, out_path: Path):
        super().__init__()
        self.client, self.config, self.jql, self.out_path = client, config, jql, out_path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @Slot()
    def run(self):
        try:
            self.log.emit("Reading field list from Jira...")
            fields, problems = resolve_fields(self.config.fields, self.client.fields())
            for p in problems:
                self.log.emit(f"Warning: {p}")
            if not fields:
                self.failed.emit("None of the configured fields were found in Jira.")
                return
            self.log.emit(f"Exporting {len(fields)} columns: "
                          + ", ".join(f.header for f in fields))
            ids = [f.id for f in fields if f.id != "key"] or ["summary"]
            issues = self.client.search(
                self.jql, ids, self.config.page_size,
                progress=lambda done, total: self.progress.emit(done, total),
                cancelled=lambda: self._cancel,
            )
            count = write_xlsx(self.out_path, fields, issues)
            if self._cancel:
                self.log.emit("Export cancelled; the file holds the issues fetched so far.")
            self.finished.emit(count, str(self.out_path))
        except (JiraError, ConfigError) as exc:
            self.failed.emit(str(exc))
        except PermissionError:
            self.failed.emit(f"Cannot write {self.out_path}. Is it open in Excel?")
        except Exception as exc:
            self.failed.emit(f"Unexpected error: {exc}")


def _start(worker: QObject, owner: QObject) -> QThread:
    """Run `worker.run` on a background thread so the window stays responsive."""
    thread = QThread(owner)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


# ------------------------------------------------------------------ pages ---

class LoginPage(QWidget):
    logged_in = Signal(object, dict)

    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings

        self.url = QLineEdit(settings.value("url", ""))
        self.url.setPlaceholderText("https://jira.yourcompany.com or https://yoursite.atlassian.net")
        self.url.editingFinished.connect(self._guess_deployment)
        self.deployment = QComboBox()
        self.deployment.addItem("Jira Data Center / Server", DATA_CENTER)
        self.deployment.addItem("Jira Cloud", CLOUD)
        saved_url = settings.value("url", "")
        # Settings saved before Cloud support have no type; guess it from the URL.
        initial = settings.value("deployment") if settings.contains("deployment") \
            else guess_deployment(saved_url) if saved_url else DATA_CENTER
        self.deployment.setCurrentIndex(max(0, self.deployment.findData(initial)))
        # True while "Cloud" is selected only because the URL ends in .atlassian.net.
        # A Cloud choice made by hand (e.g. a custom domain) is never undone by the URL.
        self._cloud_from_url = initial == CLOUD and guess_deployment(saved_url) == CLOUD
        self.deployment.activated.connect(self._type_picked)
        self.deployment.currentIndexChanged.connect(self._update_labels)
        self.username = QLineEdit(settings.value("username", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.use_token = QCheckBox("Use a Personal Access Token instead of a password")
        self.use_token.setChecked(settings.value("use_token", False, type=bool))
        self.use_token.toggled.connect(self._update_labels)
        self.verify_ssl = QCheckBox("Verify SSL certificate")
        self.verify_ssl.setChecked(settings.value("verify_ssl", True, type=bool))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._connect)
        self.password.returnPressed.connect(self._connect)

        form = QFormLayout()
        form.addRow("Jira URL", self.url)
        form.addRow("Jira type", self.deployment)
        self.user_label = QLabel()
        form.addRow(self.user_label, self.username)
        self.secret_label = QLabel()
        form.addRow(self.secret_label, self.password)
        form.addRow("", self.use_token)
        form.addRow("", self.verify_ssl)
        self.form = form

        title = QLabel("<h2>Connect to Jira</h2>")
        box = QVBoxLayout(self)
        box.addStretch()
        box.addWidget(title)
        box.addLayout(form)
        box.addWidget(self.connect_btn)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setOpenExternalLinks(True)
        box.addWidget(self.hint)
        box.addWidget(self.status)
        box.addStretch()
        self._update_labels()

    def _is_cloud(self) -> bool:
        return self.deployment.currentData() == CLOUD

    def _type_picked(self, _index):
        self._cloud_from_url = False

    def _guess_deployment(self):
        url = self.url.text().strip()
        if not url:
            return
        if guess_deployment(url) == CLOUD:
            self.deployment.setCurrentIndex(self.deployment.findData(CLOUD))
            self._cloud_from_url = True
        elif self._cloud_from_url:
            self.deployment.setCurrentIndex(self.deployment.findData(DATA_CENTER))
            self._cloud_from_url = False

    def _update_labels(self, *_):
        cloud = self._is_cloud()
        token = self.use_token.isChecked() and not cloud
        self.form.setRowVisible(self.use_token, not cloud)
        self.user_label.setText("Email" if cloud else "Username")
        self.username.setEnabled(not token)
        self.secret_label.setText("API token" if cloud else ("Token" if token else "Password"))
        self.hint.setText(
            "Jira Cloud needs an API token instead of your password. Create one at "
            "<a href='https://id.atlassian.com/manage-profile/security/api-tokens'>"
            "id.atlassian.com → Security → API tokens</a>." if cloud else "")

    def _connect(self):
        url = self.url.text().strip()
        user = self.username.text().strip()
        secret = self.password.text()
        cloud = self._is_cloud()
        token = self.use_token.isChecked() and not cloud
        if not url or not secret or (not token and not user):
            what = "email and API token" if cloud else "username and password (or token)"
            self.status.setText(f"Please fill in the URL, {what}.")
            return
        self.settings.setValue("url", url)
        self.settings.setValue("username", user)
        self.settings.setValue("use_token", self.use_token.isChecked())
        self.settings.setValue("deployment", self.deployment.currentData())
        self.settings.setValue("verify_ssl", self.verify_ssl.isChecked())

        self.connect_btn.setEnabled(False)
        self.status.setText("Connecting...")
        creds = Credentials(url, user, secret, token, self.verify_ssl.isChecked(),
                            CLOUD if cloud else DATA_CENTER)
        self._worker = LoginWorker(creds)
        self._worker.succeeded.connect(self._ok)
        self._worker.failed.connect(self._fail)
        self._thread = _start(self._worker, self)

    def _done(self):
        self._thread.quit()
        self.connect_btn.setEnabled(True)

    def _ok(self, client, user):
        self._done()
        self.password.clear()  # the password is never stored
        self.status.clear()
        self.logged_in.emit(client, user)

    def _fail(self, msg):
        self._done()
        self.status.setText(f"<span style='color:#c0392b'>{msg}</span>")


class ExportPage(QWidget):
    logout = Signal()

    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.client: JiraClient | None = None

        self.user_label = QLabel()
        logout_btn = QPushButton("Log out")
        logout_btn.clicked.connect(self.logout.emit)
        top = QHBoxLayout()
        top.addWidget(self.user_label, 1)
        top.addWidget(logout_btn)

        self.jql = QPlainTextEdit(settings.value("jql", "project = ABC ORDER BY created DESC"))
        self.jql.setFixedHeight(70)

        self.config_path = QLineEdit(settings.value("config_path", str(default_config_path())))
        browse_cfg = QPushButton("Browse...")
        browse_cfg.clicked.connect(self._pick_config)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.config_path, 1)
        cfg_row.addWidget(browse_cfg)

        default_out = Path(settings.value("out_dir", str(Path.home()))) / "jira_export.xlsx"
        self.out_path = QLineEdit(str(default_out))
        browse_out = QPushButton("Browse...")
        browse_out.clicked.connect(self._pick_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_path, 1)
        out_row.addWidget(browse_out)

        form = QFormLayout()
        form.addRow("JQL query", self.jql)
        form.addRow("Fields config", cfg_row)
        form.addRow("Save to", out_row)

        self.export_btn = QPushButton("Export to Excel")
        self.export_btn.clicked.connect(self._export)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        btns = QHBoxLayout()
        btns.addWidget(self.export_btn)
        btns.addWidget(self.cancel_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        box = QVBoxLayout(self)
        box.addLayout(top)
        box.addLayout(form)
        box.addLayout(btns)
        box.addWidget(self.progress)
        box.addWidget(self.log, 1)

    def set_session(self, client: JiraClient, user: dict):
        self.client = client
        name = user.get("displayName") or user.get("name", "")
        kind = "Jira Cloud" if client.is_cloud else "Jira Data Center"
        self.user_label.setText(
            f"Connected to <b>{client.base_url}</b> ({kind}) as <b>{name}</b>")
        self.log.clear()

    def _pick_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Fields config", self.config_path.text(),
                                              "JSON (*.json)")
        if path:
            self.config_path.setText(path)

    def _pick_output(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        start = str(Path(self.out_path.text()).parent / f"jira_export_{stamp}.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Save Excel file", start, "Excel (*.xlsx)")
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self.out_path.setText(path)

    def _export(self):
        jql = self.jql.toPlainText().strip()
        if not jql:
            QMessageBox.warning(self, "JQL needed", "Enter a JQL query to choose the issues.")
            return
        try:
            config = load_config(Path(self.config_path.text()))
        except ConfigError as exc:
            QMessageBox.critical(self, "Config problem", str(exc))
            return
        out = Path(self.out_path.text()).expanduser()
        if not out.parent.is_dir():
            QMessageBox.critical(self, "Folder missing", f"Folder does not exist: {out.parent}")
            return

        self.settings.setValue("jql", jql)
        self.settings.setValue("config_path", self.config_path.text())
        self.settings.setValue("out_dir", str(out.parent))

        self.log.clear()
        self.progress.setRange(0, 0)  # busy until the first page reports a total
        self.export_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._worker = ExportWorker(self.client, config, jql, out)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self.log.appendPlainText)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread = _start(self._worker, self)

    def _cancel(self):
        if getattr(self, "_worker", None):
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)

    def _on_progress(self, done, total):
        if total <= 0:  # total unknown (Cloud count unavailable): stay in busy mode
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"{done} issues")
            return
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done} / {total} issues")

    def _reset(self):
        self._thread.quit()
        self.export_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)

    def _on_finished(self, count, path):
        self._reset()
        self.progress.setRange(0, max(count, 1))
        self.progress.setValue(count)
        self.progress.setFormat(f"{count} issues")
        self.log.appendPlainText(f"Done: {count} issues written to {path}")
        QMessageBox.information(self, "Export complete", f"{count} issues saved to:\n{path}")

    def _on_failed(self, msg):
        self._reset()
        self.log.appendPlainText(f"Error: {msg}")
        QMessageBox.critical(self, "Export failed", msg)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Jira Desktop Exporter {__version__}")
        self.resize(720, 520)
        settings = QSettings("JiraDesktopExporter", "JiraDesktopExporter")
        self.stack = QStackedWidget()
        self.login = LoginPage(settings)
        self.export = ExportPage(settings)
        self.stack.addWidget(self.login)
        self.stack.addWidget(self.export)
        self.setCentralWidget(self.stack)
        self.login.logged_in.connect(self._on_login)
        self.export.logout.connect(self._on_logout)

    def _on_login(self, client, user):
        self.export.set_session(client, user)
        self.stack.setCurrentWidget(self.export)

    def _on_logout(self):
        self.export.client = None
        self.stack.setCurrentWidget(self.login)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
