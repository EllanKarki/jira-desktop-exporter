# Jira Desktop Exporter

A desktop app (Windows and macOS) for Jira users with normal Jira access, on **Jira Data Center/Server** or **Jira Cloud**. Log in with your own Jira URL and credentials, run a JQL query, and export the matching issues to an Excel file. The columns come from a config file.

## Features

- **Login screen:** Jira URL, Jira type and credentials. The password or token is never saved; the URL, type and username are remembered.
  - **Data Center / Server:** username and password, or a Personal Access Token (Data Center 8.14+).
  - **Cloud:** email and an [API token](https://id.atlassian.com/manage-profile/security/api-tokens). Cloud doesn't accept account passwords for API access.
  - The type is picked automatically when the URL ends in `.atlassian.net`, and you can change it for Cloud sites on a custom domain.
- **Excel export:** enter any JQL query and the app fetches every matching issue page by page, writing them to `.xlsx` with a styled header, filters and real date cells.
- **Configurable fields:** `config/fields.json` sets which fields are exported and in what order.
- **Menus:** **Jira** (Login, Logout, Exit), **Configuration** (Discover fields), **Extract** (JQL Extract), **Jira Actions** (Create Sprint) and **Reports** (Sprint Report, Velocity Chart, Burndown Chart). The app opens on a start screen. The login form appears only when you choose **Jira > Login**, and the export form only when you choose **Extract > JQL Extract**; after logging in or out you're back on the start screen. Items that need a Jira connection are greyed out until you log in, and Logout is greyed out while an export runs.
- **Discover fields:** pick a project and the app lists the fields it uses, so you can tick the ones you want and save them as a `fields.json`.
- **Create Sprint:** pick a team, project and sprint number, and the app creates the sprint on that team's Scrum board, using the settings in `config/sprints.xlsx`.
- **Sprint Report:** a table of each team's recent closed sprints: committed, completed and spilled issues and story points, how many issues changed during the sprint, and the completion percentage. Export it to Excel.
- **Velocity Chart:** a bar chart of one team's committed vs completed story points over its recent closed sprints, with the average velocity. Export it to Excel (with the chart) or save the chart as an image.
- **Burndown Chart:** one sprint's remaining story points over time against a straight guideline, with a day-by-day table. Export it to Excel (with the chart) or save the chart as an image.

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

Instead of looking up field ids by hand, choose **Configuration > Discover fields**:

1. Enter a project key (for example `ABC`) and click **Scan**.
2. The app combines three things: every field Jira knows about, the fields on the project's create screens (per issue type), and which fields are actually filled in on the project's most recently updated issues (200 by default; set **Issues to check** to 0 to skip this).
3. The table shows each field's name, id, type, how many of the checked issues have a value, and which issue types offer it. Fields with values are ticked to start with. **Tick all** ticks every field listed (respecting the filter), and **Untick all** clears them. Use the filter box to find a field, and tick **Show fields the project doesn't use** to see everything.
4. Click **Save as fields.json...**. The ticked fields are written by `id` with the Jira name as the column `header`, other settings (like `max_results_per_page`) are kept from the config you had open, and the export screen switches to the new file. Edit it afterwards to reorder or rename columns.

**Export field list to Excel...** saves the whole table (used and unused fields) as a reference.

Notes:

- The scan only sees what your Jira account can see, like the rest of the app.
- Create screens are read with `/issue/createmeta/{project}/issuetypes`, which Data Center added in 8.4. On older versions the app says so and relies on the sampled issues.
- A field that is filled in only on older issues won't show as used unless you raise **Issues to check**.

The app looks for `config/fields.json` next to the app first. You can also pick any config file with **Browse...** on the export screen.

## Creating sprints

Choose **Jira Actions > Create Sprint...** after logging in.

### The sprint config (`config/sprints.xlsx`)

One row per team and project on the **Teams** sheet. A team can have several rows, one per project. The **Help** sheet in the file explains each column.

| Column | Required | Example | What it is |
|---|---|---|---|
| Team | yes | Team Alpha | Shown in the form's Team list. |
| Project Key | yes | ABC | Shown in the Project list for that team. |
| Board ID | yes | 12 | The Scrum board the sprint is created on. Open the board in Jira; the number after `rapidView=` (Data Center) or `/boards/` (Cloud) in the address is its ID. |
| Sprint Name Format | no | `{team} Sprint {number}` | Placeholders: `{team}`, `{project}`, `{number}`. This is also the default. `{number}` is written with at least two digits, so sprint 1 is named "Team Alpha Sprint 01". |
| First Sprint Number | yes | 1 | The number of a sprint whose start date you know. |
| First Sprint Start Date | yes | 2026-01-05 | That sprint's start date: an Excel date, or text written year-month-day. Text like 01/05/2026 is refused because it means different days in different countries. |
| Sprint Length (days) | yes | 14 | Sprints follow on back to back: sprint N starts (N − first number) × length days after the first one, and ends when the next one starts. |
| Start Time | no | 09:00 | Start and end time of day, in your computer's time zone. Default 09:00. |
| Sprint Goal | no | `Sprint {number} goal` | Same placeholders as the name. |

Rows with problems (an empty required cell, a date that isn't a date, and so on) are skipped and listed in the form. If the same team and project appear twice, the first row is used. **New from template...** saves an example file to fill in.

### The form

The form opens with the config next to the app (or the last one you picked). Choose the **Team**, **Project** and **Sprint number**. The sprint number is typed as text and keeps leading zeros: `1` becomes `01`, and typing `001` keeps three digits. The name, start, end and goal are filled in from the config, and you can change them before creating. **Create Sprint** then:

1. checks the board exists and isn't a Kanban board, and that it has no sprint with the same name (any state);
2. asks you to confirm, noting if the board belongs to a different project;
3. creates the sprint in the **future** state. It is not started; start it from the board in Jira as usual.

After creating, the form moves on to the next sprint number at the same width (`01` → `02`, `09` → `10`), and remembers it for that team and project. Other teams and projects start at the config's First Sprint Number until you create one there.

Sprint names are limited to 30 characters, the limit Atlassian documents in JSWCLOUD-17483 and JSWSERVER-16256. You need permission to manage sprints on the board's projects (**Manage Sprints**; Jira Cloud may also require **Schedule Issues**). Without it, Jira refuses and the form shows its error.

## Sprint Report

Choose **Reports > Sprint Report...** after logging in. It uses the same `config/sprints.xlsx` as Create Sprint: each team's **Board ID** says where its sprints are.

Pick a team (or **All teams**) and how many of each board's most recent closed sprints to include, then click **Run report**. Each row is one team and sprint:

| Column | What it counts |
|---|---|
| Team Name | The team from the sprint config. |
| Sprint Name | The sprint's name in Jira. |
| Committed Issues / Story Points | Issues in the sprint when it started, with their estimates at the start. |
| Completed Issues / Story Points | Issues done when the sprint closed, with their final estimates. Issues added after the start count here too, as in Jira's own sprint report. |
| Spill Over Issues / Story Points | Issues still not done when the sprint closed. |
| Issues Adjusted During Sprint | Issues added after the start plus issues removed before the end. An issue added and then removed counts twice. |
| % Completed vs Committed | Completed story points ÷ committed story points. It can go over 100% when work was added, and is empty when nothing was estimated at the start. |

Click a column header to sort, and **Export to Excel...** to save the table.

Notes:

- The numbers come from the same data as Jira's own Sprint Report on the board (`/rest/greenhopper/1.0/rapid/charts/sprintreport`). Atlassian doesn't document that endpoint as a public API, so a future Jira version could change it.
- "Story points" are whatever the board estimates with (**Board settings > Estimation**), usually the Story Points field. The status line names the field.
- Only sprints created on the team's board are listed, not sprints from other boards that share its issues. Active and future sprints are left out.
- Issues Jira lists as "completed outside this sprint" are left out of every column.

## Velocity Chart

Choose **Reports > Velocity Chart...** after logging in. Like the Sprint Report it reads the teams and boards from `config/sprints.xlsx`.

Pick a team and how many of its most recent closed sprints to show (7 to start with), then click **Run report**. For each sprint the chart shows two bars:

- **Committed:** story points in the sprint when it started, with their estimates at the start.
- **Completed:** story points of the issues done when the sprint closed, including issues added after the start.

These are the same numbers as the Sprint Report's committed and completed story points, so the two reports always agree. The table under the chart lists them with the completion percentage, and the status line gives the **average velocity**: the mean completed story points per sprint shown. When every sprint name starts with the team name, the chart leaves it off the labels ("Sprint 01").

**Export to Excel...** saves the table, the average and a bar chart that Excel draws from the table. **Save chart image...** saves the chart as a PNG to paste into a slide or an email.

The notes under the Sprint Report about where the numbers come from apply here too.

## Burndown Chart

Choose **Reports > Burndown Chart...** after logging in. Pick a team (from `config/sprints.xlsx`); the **Sprint** list then fills with that team's board's active and closed sprints, newest first. Pick one and click **Run report**.

- **Remaining** (red line): story points of the sprint's issues that aren't done yet. It steps down when an issue is done, and up when an issue is added, re-estimated upwards or reopened.
- **Guideline** (dashed): a straight line from the story points at the start to zero at the sprint's planned end.
- For a closed sprint the line stops when it was closed; for an active one it stops now.

The table lists each day with the remaining story points at the end of the day, the guideline, the story points completed that day, and the scope change that day (added or removed issues and re-estimates; positive means more work).

**Export to Excel...** saves the table with a line chart of remaining vs guideline. **Save chart image...** saves the chart as a PNG.

Notes:

- The numbers come from the same data as Jira's own Burndown Chart (`/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart`), which Atlassian doesn't document as a public API, like the Sprint Report's.
- An issue counts as done when it reaches the board's last column, as in Jira's chart. "Story points" are the board's estimate field.
- Days follow your computer's time zone.

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
- **Create Sprint** uses the Jira Software Agile REST API on both Cloud and Data Center: `GET /rest/agile/1.0/board/{id}`, `GET /rest/agile/1.0/board/{id}/sprint` and `POST /rest/agile/1.0/sprint`.
- **Rate limits:** if Jira answers 429 ("Rate limit exceeded", which Data Center sends when an admin has turned on rate limiting, and Cloud uses too), the app waits as long as Jira's `Retry-After` header asks (or 2, 4, 8... seconds, at most 60 per wait) and retries up to 6 times, showing that it is waiting. If the limit still doesn't clear, it says so; your Jira admin can raise the limit for your account.
- Jira Cloud rejects unbounded JQL (for example `ORDER BY created` on its own). Add a condition such as `project = ABC`.
- Data Center: if your company uses SSO-only login or has disabled basic auth, password login won't work. Use a Personal Access Token instead (Profile → Personal Access Tokens in Jira).
- If Jira has shown you a CAPTCHA after failed logins, log in once in the browser first.
- If you use an internal certificate authority, untick **Verify SSL certificate**. Only do this on a network you trust.
