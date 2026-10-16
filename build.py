"""Build a standalone app with PyInstaller for the current OS (run on Windows or macOS).

    pip install -r requirements-dev.txt
    python build.py

Output goes to dist/. An editable copy of config/fields.json is placed next to the app.
"""
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
NAME = "JiraDesktopExporter"

PyInstaller.__main__.run([
    str(ROOT / "src" / "jira_exporter" / "__main__.py"),
    "--name", NAME,
    "--windowed",
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT / "src"),
    "--add-data", f"{ROOT / 'config'}{os.pathsep}config",
])

dist = ROOT / "dist"
target = dist / NAME if (dist / NAME).is_dir() else dist
shutil.copytree(ROOT / "config", target / "config", dirs_exist_ok=True)
print(f"Built for {sys.platform}: {target}")
