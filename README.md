# Jira Desktop Exporter

A desktop app (Windows and macOS) for Jira users with normal Jira access, on **Jira Data Center/Server** or **Jira Cloud**. Log in with your own Jira URL and credentials, run a JQL query, and export the matching issues to an Excel file. The columns come from a config file.

## Features

- **Login screen:** Jira URL, Jira type and credentials. The password or token is never saved; the URL, type and username are remembered.
  - **Data Center / Server:** username and password, or a Personal Access Token (Data Center 8.14+).
  - **Cloud:** email and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens). Cloud doesn't accept account passwords for API access.
  - The type is picked automatically when the URL ends in `.atlassian.net`, and you can change it for Cloud sites on a custom domain.
- **Excel export:** enter any JQL query and the app fetches every matching issue page by page, writing them to `.xlsx` with a styled header, filters and real date cells.
- **Configurable fields:** `config/fields.json` sets which fields are exported and in what order.
- **Menus:** **Jira** (Login, Logout, Exit), **Configuration** (Discover fields) and **Extract** (JQL Extract). The app opens on a start screen, and the login form appears only when you choose **Jira > Login** (and again after you log out). Items that need a Jira connection are greyed out until you log in, and Logout is greyed out while an export runs.
- **Discover fields:** pick a project and the app lists the fields it uses, so you can tick the ones you want and save them as a `fields.json`.

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

Fields that Jira doesn't know about are skipped, and a warning appears in the log.

### Discover a project's fields

Instead of looking up field ids by hand, click **Discover fields...** on the export screen:

1. Enter a project key (for example `ABC`) and click **Scan**.
2. The app combines three things: every field Jira knows about, the fields on the project's create screens (per issue type), and which fields are actually filled in on the project's most recently updated issues (200 by default; set **Issues to check** to 0 to skip this).
3. The table shows each field's name, id, type, how many of the checked issues have a value, and which issue types offer it. Fields with values are ticked to start with. Use the filter box to find a field, and tick **Show fields the project doesn't use** to see everything.
4. Click **Save as fields.json...**. The ticked fields are written by `id` with the Jira name as the column `header`, other settings (like `max_results_per_page`) are kept from the config you had open, and the export screen switches to the new file. Edit it afterwards to reorder or rename columns.

**Export field list to Excel...** saves the whole table (used and unused fields) as a reference.

Notes:

- The scan only sees what your Jira account can see, like the rest of the app.
- Create screens are read with `/issue/createmeta/{project}/issuetypes`, which Data Center added in 8.4. On older versions the app says so and relies on the sampled issues.
- A field that is filled in only on older issues won't show as used unless you raise **Issues to check**.

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

The tests run against a local fake Jira server, including a headless run of the real window: log in, export 250 issues across 3 pages, and check the Excel file. They also scan a project with **Discover fields**, save a `fields.json` from it and export with that file.

## Notes and limits

- **Data Center** uses REST API v2: `/rest/api/2/myself`, `/field`, `/search` (pages with `startAt`). **Discover fields** also uses `/project/{key}` and `/issue/createmeta/{key}/issuetypes[/{id}]`.
- **Cloud** uses REST API v3: `/rest/api/3/myself`, `/field`, `/project/{key}`, `/issue/createmeta/{key}/issuetypes[/{id}]`, `/search/jql` (pages with `nextPageToken`), and `/search/approximate-count` for the progress bar. The older `/rest/api/3/search` has been removed from Cloud. Rich-text fields such as Description come back from Cloud in Atlassian Document Format and are converted to plain text.
- Jira Cloud rejects unbounded JQL (for example `ORDER BY created` on its own). Add a condition such as `project = ABC`.
- Data Center: if your company uses SSO-only login or has disabled basic auth, password login won't work. Use a Personal Access Token instead (Profile → Personal Access Tokens in Jira).
- If Jira has shown you a CAPTCHA after failed logins, log in once in the browser first.
- If you use an internal certificate authority, untick **Verify SSL certificate**. Only do this on a network you trust.
