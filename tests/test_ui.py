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
