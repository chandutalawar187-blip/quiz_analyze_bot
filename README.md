# Quizcord Analytics Bot

A production-structured Discord bot for linking server members to student
identifiers, importing Quizcord leaderboards, and privately viewing scores and
quiz attendance.

## Project structure

```text
.
├── .env.example
├── pyproject.toml
├── src/
│   └── quizcord_bot/
│       ├── cogs/analytics.py
│       ├── bot.py
│       ├── config.py
│       ├── database.py
│       ├── quizcord.py
│       └── views.py
└── tests/
```

The existing `fetch_users.py` remains as a small compatibility launcher.

## Requirements and setup

- Python 3.10 or newer
- A Discord application/bot with the **Server Members Intent** enabled in the
  Discord Developer Portal
- The **Message Content Intent** enabled in both the Discord Developer Portal
  and the bot configuration; Discord gates message embed data behind this
  privileged intent.
- The bot invited with application-command and member permissions

The bot reads Quizcord leaderboard embeds, so it needs Message Content Intent
even though it does not parse ordinary message text.

In PowerShell, create the private environment file, edit in your real values,
and install the project:

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
python -m pip install -e .
python -m quizcord_bot
```

Alternatively, keep using the compatibility launcher:

```powershell
python .\fetch_users.py
```

`.env` is excluded from version control. Never commit a live Discord bot token.
If a token has previously been shared or committed, reset it in the Discord
Developer Portal.

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token. |
| `QUIZCORD_BOT_ID` | No | Optional fixed Quizcord bot ID. Leave blank to detect bot/webhook quiz-start, leaderboard, and Quiz ID signals automatically. |
| `BOT_GUILD_ID` | Yes | The one server where slash commands and automatic Quizcord imports are enabled. |
| `BOT_DATABASE_PATH` | No | SQLite database path. Defaults to `server_results.db` in the project directory. |
| `DASHBOARD_PUBLIC_URL` | No | Public HTTPS origin for the web dashboard, e.g. `https://analytics.example.com`. |
| `DISCORD_OAUTH_CLIENT_ID` | With dashboard | Discord application's OAuth2 client ID. |
| `DISCORD_OAUTH_CLIENT_SECRET` | With dashboard | Discord application's OAuth2 client secret. |
| `DASHBOARD_SESSION_SECRET` | With dashboard | Unique session-signing secret, at least 32 characters. |
| `FACULTY_ROLE_ID` | With faculty dashboard | Discord server role ID granted to faculty; leave blank to disable faculty access. |
| `DASHBOARD_HOST` | No | Bind address; defaults to `127.0.0.1` behind a local reverse proxy. |
| `DASHBOARD_PORT` | No | Web server port; defaults to `8765`. |

### Faculty dashboard

Create a dedicated `Faculty` role in the configured Discord server and assign
it only to faculty members. Enable Discord Developer Mode, copy the role ID,
and set it as `FACULTY_ROLE_ID`. Faculty sign in with Discord; the dashboard
checks their current server role through OAuth and requires the configured
role. The application checks the member's current roles through Discord's
server API using the configured bot token; no additional member OAuth scope is
required. The bot must remain a member of the configured server. Administrators
retain access to the existing admin dashboard. Faculty use a separate branded
sign-in page at `/faculty/login`: they authenticate with Discord first, then
enter an app-managed faculty password. The application never requests or stores
a Discord account password.
Register `https://<your-dashboard-domain>/auth/callback` as an exact OAuth2
redirect URI in the same Discord application configured by
`DISCORD_OAUTH_CLIENT_ID` and `DISCORD_OAUTH_CLIENT_SECRET`. If the callback
reports an authorization-code exchange failure, verify that URI, the dashboard
public URL, and the application credentials match; restart sign-in after
correcting configuration so Discord issues a fresh authorization code.

Each faculty account manages its own student list on `/faculty`. Faculty can
search the synchronized human-member directory by USN and add or remove
students. A student is available for assignment when their saved USN exists or
their global display name matches an accepted USN format. Faculty can see
attendance and performance only for students in their own list; assignments
are isolated by faculty account and server.

Administrators can open **Faculty passwords** from the admin dashboard or visit
`/admin/faculty-passwords` to issue or reset a faculty member's app password.
The member must have the configured Faculty role. The temporary password is
shown only once; share it securely. On first use, faculty must replace it with
a password of at least 12 characters. Passwords are stored as salted PBKDF2
hashes, and five consecutive failed attempts lock password verification for
15 minutes. To reset a forgotten password, an administrator issues a new
temporary password.

### Enable the web dashboard

The dashboard is served by the same process as the Discord bot. Railway is a
supported single-service deployment:

1. For the initial deployment, install Railway's CLI and deploy this project
   directory from PowerShell:

   ```powershell
   npm install -g @railway/cli
   railway login
   Set-Location C:\Users\chand\Desktop\discord_bot
   railway up
   ```

   `railway up` deploys the current directory. `.env`, `.venv`, and local
   database files are excluded by `.gitignore`; do not use the CLI's
   `--no-gitignore` option.

   To deploy automatically instead of running `railway up` each time:

   - Push this project to GitHub on the `main` branch. The workflow at
     `.github/workflows/deploy-railway.yml` deploys each push to Railway and
     can also be started manually from the repository's **Actions** tab.
   - In Railway, create a **project token** for the project/environment that
     hosts this bot. In the GitHub repository, open **Settings → Secrets and
     variables → Actions → New repository secret**, name it exactly
     `RAILWAY_TOKEN`, and paste the project token as its value. Do not put the
     Railway token in source code, `.env`, or a GitHub Actions variable.
     The workflow checks that this secret is present before invoking Railway.
   - If your deployment branch is not `main`, update the workflow's branch
     filter to match it. The automated workflow requires the repository to be
     pushed to GitHub and this project to be linked/configured in Railway.

2. In Railway, open the created service and add a **Volume** mounted at
   `/data`. Set `BOT_DATABASE_PATH=/data/server_results.db`.
3. In the service's **Settings → Deploy**, set the start command to
   `python railway_start.py`. This launcher adds the project's `src` directory
   to Python's import path before starting the bot. The Railway requirements
   file lists runtime dependencies without installing the local package in
   Railpack's dependency-only build layer. Keep only one replica because
   SQLite and in-memory OAuth sessions are intended for a single running
   instance.
4. Under **Settings → Networking**, generate a Railway domain. Railway gives
   the service HTTPS and injects the port as `PORT`; this app reads that
   variable automatically. In the service variables, set:

   ```powershell
   DASHBOARD_PUBLIC_URL=https://your-service.up.railway.app
   DASHBOARD_HOST=0.0.0.0
   BOT_DATABASE_PATH=/data/server_results.db
   ```

   Replace the example domain with the exact Railway HTTPS domain. Do not set
   `DASHBOARD_PORT`; Railway supplies `PORT`.
5. In the Discord Developer Portal, open the same Discord application used by
   the bot. Set `DISCORD_OAUTH_CLIENT_ID` to its **Application ID**, create an
   OAuth2 client secret, and set it as `DISCORD_OAUTH_CLIENT_SECRET`. Add this
   exact redirect URL under OAuth2 redirects:

   ```text
   https://your-service.up.railway.app/auth/callback
   ```

6. Generate a long random secret on your own computer:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Set the output as `DASHBOARD_SESSION_SECRET` in Railway. Also set
   `DISCORD_BOT_TOKEN`, `BOT_GUILD_ID`, and optionally `QUIZCORD_BOT_ID` in
   Railway's service variables. Never paste these secrets into source files,
   chat, or a public repository.
7. Apply the staged variable changes and deploy/restart the service. Check
   Railway's deployment logs, then open
   `https://your-service.up.railway.app/health`; it should return
   `{"status":"ok"}`. Sign in at the base URL with a Discord account that owns
   the server or has **Manage Server** / **Administrator** permission.

At quiz start, the server owner receives a private Discord DM with an **Open
live web dashboard** button. The dashboard refreshes every few seconds and
shows the roster, final analytics, and Excel download.

If you already have student mappings/results in a local `server_results.db`,
stop the local bot and back up the database before moving it. Upload that file
to the Railway volume at `/server_results.db` (the volume's root path; it
appears to the app as `/data/server_results.db`) before relying on the hosted
service's database. Otherwise, the hosted bot will initialize a new database
and you will need to recreate the mappings.

Railway's volume prevents database loss on redeploy, but OAuth sessions are
held in memory; they expire after an hour and are cleared on restart. Sign in
again after a deployment. Railway-hosted services and storage may incur charges;
check the current Railway plan and usage before leaving the service running.

For a local machine with a separately configured HTTPS reverse proxy, use
`DASHBOARD_HOST=127.0.0.1` and `DASHBOARD_PORT=8765` instead of Railway's host
and injected `PORT` settings. If the dashboard variables are omitted, the bot
continues to use its private Discord dashboard.

## Features

- `/map_student`: Link a server member to a student identifier and display
  name; optionally provide their Quizcord leaderboard nickname so it can be
  matched even when it differs from their Discord username/nickname.
- `/process_quizcord`: Import leaderboard text and privately open scorecards
  for every listed student. Rows without a unique student mapping are shown
  using their leaderboard nickname/USN. When the quiz start was tracked, its
  start date and time are saved automatically. Its optional `quiz_date` field
  accepts `YYYY-MM-DD` for a date override; if the start was not tracked and
  no override is provided, the import timestamp is saved.
- `/export_analytics`: Download the private aggregate Excel workbook (`.xlsx`).
- Detect the Quizcord question-one/start signal and privately notify the server
  owner with the web dashboard link when configured. If no start signal is
  visible, monitoring begins at the first leaderboard. The same leaderboard
  message is observed when edited; each distinct revision updates the roster
  and question-update count.
- The dashboard refreshes live leaderboard data and links each participant
  USN to an individual student page. Student pages show date-wise quiz
  attendance, completed-quiz attendance percentage with a circular progress
  chart, and Quizcord points by quiz. Each student page offers an Excel
  download with that student's summary and date-wise quiz history. The roster
  and student pages do not expose internal mapping status; final quiz data
  can also be exported to Excel. Administrators can open the **All users**
  dashboard tab to search the configured server's synchronized human members,
  see their global display name, account username, server nickname, and student
  mapping, and identify users whose global display name is missing or does not
  match an accepted USN format. If a member has no saved USN mapping but their
  global display name exactly matches an accepted USN format, the directory
  shows that value as their USN; this inferred value does not create a student
  mapping or populate a student name.
  The Discord fallback buttons remain usable after a bot restart.
- Quiz attendance summaries show attendees over the human guild-member count
  and an attendance percentage. The dashboard labels this count
  "Students attending" while live and "Students attended" after completion.
  The denominator includes all human members regardless of server permissions
  and excludes bot accounts; it is snapshotted when each live quiz starts.
  Leaderboard results from owners and administrators are counted like any
  other human member. Names are matched against saved USNs and Discord or
  Quizcord aliases. Discord mention results are resolved to cached member
  display names, including when the member has no student mapping. If a
  leaderboard name contains multiple Discord mentions, a unique registered
  account among those mentions is used to associate the result with its
  existing student record instead of creating a second student. On startup,
  existing composite-mention rows are also repaired when exactly one mentioned
  account has a registered student mapping.
- Student Discord global display names are checked against `4GM` + 2 digits + `CS` + 3
  digits or `U` + 2 digits + `E` + 2 digits + `CS` + 3 digits. A mismatch
  never blocks quiz results: the dashboard shows "Name not updated to USN".
  For a mapped student, the bot sends one private reminder per quiz
  asking them to set their Discord global display name to their saved USN;
  their unique account username does not need to change. The reminder is
  skipped when Discord DMs are unavailable.
- The first usable leaderboard revision and later edits are deduplicated by
  message/revision. Unknown USNs are retained in the quiz roster; known
  students are associated internally through Discord account username, server
  nickname, or their saved Quizcord username. `@nickname` values are normalized.
- Editing a final leaderboard to add its Quiz ID updates the original quiz
  session instead of creating a second attendance record. Student history also
  collapses legacy duplicate records with matching channel, timestamp, rank,
  and points when one copy lacks the Quiz ID.
- Duel-mode manual leaderboard imports include all parsed participants in the
  private scorecard list. A row with an ambiguous or missing mapping is shown
  under its leaderboard name; mapping status is not exposed.
- The final leaderboard completes the session when labeled as final. If
  Quizcord posts the Quiz ID in a separate follow-up embed, that ID also marks
  the session complete and is attached to the final result.
- Scorecards show the student's rank, score, and the number of **distinct
  Quizcord quizzes** in which they appear in imported results. Re-importing or
  updating the same quiz does not increase the attendance count.
- Student profile attendance totals include older manually imported quiz
  results. New imports use the stored quiz start timestamp when that quiz was
  tracked, or the import timestamp when the start was not observed; `quiz_date`
  can override this with a date. Imports made before date tracking was added
  continue to show "Date unavailable" because their original quiz dates were
  not stored; the app never assigns them the date of a different or current
  quiz.
- Scorecards and Excel exports include total and average leaderboard points.
  Quizcord's leaderboard score is points, not a verified correct-answer count.
- Leaderboard buttons support pagination for larger classes.

The live attendee count is the number of distinct USNs in the latest
leaderboard snapshot. For a final quiz export, the final snapshot is
authoritative. The live question indicator counts distinct leaderboard
revisions; if Quizcord does not expose a total question count, the bot shows the
current update number rather than inventing a percentage. Once the final
leaderboard or its Quiz ID follow-up arrives, the dashboard marks the quiz
complete.

Quizcord leaderboard data provides points and rank, not a verified count of
correct answers. The dashboard and workbook therefore report Quizcord points
and clearly avoid presenting those points as correct-answer counts.

## Protections

- Management commands require **Manage Server** and are rate-limited.
- Application commands and automatic imports are restricted to the configured
  server.
- Scorecards, manual-import dashboards, and live quiz notifications are private
  to their recipient; automatic notifications go to the server owner.
- The optional web dashboard uses Discord OAuth, server-admin authorization,
  expiring server-side sessions, HTTPS-only cookies, and no-cache/security
  response headers. Access is denied when current server permissions cannot
  be verified.
- Automatic monitoring is restricted to the configured server and accepts
  messages from a bot or webhook matching `QUIZCORD_BOT_ID` when configured,
  or bot/webhook question-start, leaderboard, and Quiz ID signals when it is
  blank.
- Input sizes and parsed leaderboard row counts are bounded.
- SQLite writes use parameterized queries, transactions, foreign-key checks,
  and a busy timeout. The existing database schema is migrated in place.
- Unexpected command and delivery errors are logged without exposing
  implementation details to users.

## Tests

Run the focused test suite after installing the package:

```powershell
python -m unittest discover -s tests -v
```
