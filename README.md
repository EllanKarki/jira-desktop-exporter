# Jira Desktop Exporter

A desktop app (Windows and macOS) for Jira Data Center users with normal Jira access. Log in with your own Jira URL and credentials, run a JQL query, and export the matching issues to an Excel file. The columns come from a config file.

## Features

- **Login screen:** Jira URL, username and password. You can also use a Personal Access Token if your Jira allows it (Data Center 8.14+). The password is never saved. The URL and username are remembered.
- **Excel export:** enter any JQL query and the app fetches every matching issue page by page, writing them to `.xlsx` with a styled header, filters and real date cells.
- **Configurable fields:** `config/fields.json` sets which fields are exported and in what order.

## Configuring fields

Edit `config/fields.json`. Each entry names a field by its display `name` as shown in Jira, or by its `id` (needed for custom fields that share a name). `header` optionally renames the Excel column.

```json
{
  "max_results_per_page": 100,
  "fields": [
    { "id": "key", "header": "Issue Key" },
    { "name": "Summary" },
    { "name": "Status" },
    { "id": "customfield_10002", "header": "Story Points" }
  ]
}
```

Fields that Jira doesn't know about are skipped, and a warning appears in the log. To find a custom field's id, open `https://<your-jira>/rest/api/2/field` in a browser while logged in.

The app looks for `config/fields.json` next to the app first. You can also pick any config file with **Browse...** on the export screen.

## Running from source

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
PYTHONPATH=src python -m jira_exporter   # Windows: set PYTHONPATH=src && python -m jira_exporter
```

## Building the app

Build on the OS you are targeting:

```bash
pip install -r requirements-dev.txt
python build.py
```

The app lands in `dist/` with an editable `config/` folder next to it. GitHub Actions also builds Windows and macOS versions on every push to `main` (see the **Actions** tab → latest run → *Artifacts*).

The builds are not code-signed. On first launch, Windows SmartScreen may ask you to confirm ("More info → Run anyway"). On macOS, right-click the app and choose **Open**.

## Tests

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen pytest -q
```

The tests run against a local fake Jira server, including a headless run of the real window: log in, export 250 issues across 3 pages, and check the Excel file.

## Notes and limits

- The app talks to Jira's REST API v2 (`/rest/api/2/myself`, `/field`, `/search`), which Jira Data Center provides.
- If your company uses SSO-only login or has disabled basic auth, password login won't work. Use a Personal Access Token instead (Profile → Personal Access Tokens in Jira).
- If Jira has shown you a CAPTCHA after failed logins, log in once in the browser first.
- If you use an internal certificate authority, untick **Verify SSL certificate**. Only do this on a network you trust.
