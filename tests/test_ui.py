"""Drives the real window headlessly: log in, export, check the file."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import load_workbook
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from jira_exporter import app as appmod

ROOT = Path(__file__).resolve().parents[1]


def wait_until(qapp, cond, timeout=10):
    import time
    end = time.time() + timeout
    while not cond() and time.time() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert cond()


def test_login_then_export(jira_server, tmp_path, monkeypatch):
    # Keep the test's saved settings out of the real user profile.
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    qapp = QApplication.instance() or QApplication([])
    win = appmod.MainWindow()

    lp = win.login
    lp.url.setText(f"http://127.0.0.1:{jira_server.server_address[1]}")
    lp.username.setText("alice")
    lp.password.setText("wrong")
    lp.connect_btn.click()
    wait_until(qapp, lambda: "Login failed" in lp.status.text())

    lp.password.setText("secret")
    lp.connect_btn.click()
    wait_until(qapp, lambda: win.stack.currentWidget() is win.export)
    assert lp.password.text() == ""

    ep = win.export
    ep.jql.setPlainText("project = ABC")
    ep.config_path.setText(str(ROOT / "config" / "fields.json"))
    out = tmp_path / "ui.xlsx"
    ep.out_path.setText(str(out))
    ep.export_btn.click()
    wait_until(qapp, lambda: "Done: 250 issues" in ep.log.toPlainText())
    log = ep.log.toPlainText()
    assert "Warning: Field name 'Priority' was not found" in log
    ws = load_workbook(out).active
    assert ws.max_row == 251
    assert ws["A1"].value == "Issue Key"


def test_cloud_login_then_export(cloud_server, tmp_path, monkeypatch):
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    qapp = QApplication.instance() or QApplication([])
    win = appmod.MainWindow()

    lp = win.login
    lp.url.setText("https://acme.atlassian.net")
    lp._guess_deployment()
    assert lp.deployment.currentData() == "cloud"
    assert lp.user_label.text() == "Email"
    assert lp.secret_label.text() == "API token"
    assert lp.use_token.isHidden()

    lp.url.setText(f"http://127.0.0.1:{cloud_server.server_address[1]}")  # stays on Cloud
    lp.username.setText("alice")
    lp.password.setText("secret")
    lp.connect_btn.click()
    wait_until(qapp, lambda: win.stack.currentWidget() is win.export)
    assert "Jira Cloud" in win.export.user_label.text()

    ep = win.export
    ep.jql.setPlainText("project = ABC")
    ep.config_path.setText(str(ROOT / "config" / "fields.json"))
    out = tmp_path / "cloud_ui.xlsx"
    ep.out_path.setText(str(out))
    ep.export_btn.click()
    wait_until(qapp, lambda: "Done: 250 issues" in ep.log.toPlainText())
    assert load_workbook(out).active.max_row == 251


def _fresh_settings(tmp_path):
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path))
    return QSettings("JiraDesktopExporter", "JiraDesktopExporter")


def test_type_guessed_from_settings_saved_before_cloud_support(tmp_path):
    s = _fresh_settings(tmp_path)
    s.setValue("url", "https://acme.atlassian.net")
    s.setValue("use_token", True)
    s.sync()
    QApplication.instance() or QApplication([])
    win = appmod.MainWindow()
    lp = win.login
    assert lp.deployment.currentData() == "cloud"
    assert lp.secret_label.text() == "API token"


def test_saved_type_wins_over_url_guess(tmp_path):
    s = _fresh_settings(tmp_path)
    s.setValue("url", "https://jira.acme.com")
    s.setValue("deployment", "cloud")  # Cloud on a custom domain, chosen by hand
    s.sync()
    QApplication.instance() or QApplication([])
    win = appmod.MainWindow()
    lp = win.login
    assert lp.deployment.currentData() == "cloud"
    lp.url.setText("https://jira2.acme.com")  # editing the URL keeps the manual choice
    lp._guess_deployment()
    assert lp.deployment.currentData() == "cloud"


def test_url_guess_switches_back_only_when_it_chose_cloud(tmp_path):
    _fresh_settings(tmp_path)
    QApplication.instance() or QApplication([])
    win = appmod.MainWindow()
    lp = win.login
    lp.url.setText("https://acme.atlassian.net")
    lp._guess_deployment()
    assert lp.deployment.currentData() == "cloud"
    lp.url.setText("https://jira.acme.com")
    lp._guess_deployment()
    assert lp.deployment.currentData() == "datacenter"
    # Picking Cloud by hand for a custom domain sticks through URL edits.
    lp.deployment.setCurrentIndex(lp.deployment.findData("cloud"))
    lp.deployment.activated.emit(lp.deployment.currentIndex())
    lp.url.setText("https://jira.acme.org")
    lp._guess_deployment()
    assert lp.deployment.currentData() == "cloud"


def test_progress_without_total_stays_busy(tmp_path):
    _fresh_settings(tmp_path)
    QApplication.instance() or QApplication([])
    win = appmod.MainWindow()
    ep = win.export
    ep._on_progress(100, 0)
    assert ep.progress.maximum() == 0
    assert ep.progress.format() == "100 issues"
    ep._on_progress(100, 250)
    assert ep.progress.maximum() == 250
