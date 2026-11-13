# Lead workbench

A local record of LinkedIn networking research: who you found, the evidence behind each fact,
the invitation draft, what happened to the invitation, and which companies rejected an
application and are worth reaching through a person.

It records research and completed actions; it never contacts LinkedIn, sends invitations or
scrapes. Local mode makes no outbound requests. Optional Google Drive sync sends your records
to the account you explicitly connect. Python 3.10+ standard library only; no dependencies,
CDN or telemetry. Codex and Claude Code plugins share the same backend and private data directory.

- [Install the local plugins](docs/PLUGINS.md)
- [Optional Google Drive sync and multi-laptop behavior](docs/SYNC.md)
- [Future features, including optional Gmail import](docs/ROADMAP.md)

## Start

```sh
git clone https://github.com/cristiangirlea/lead-workbench.git
cd lead-workbench
python -m app init
python -m app serve            # http://127.0.0.1:8765/  (Ctrl+C to stop; loopback only)
```

## CLI (every command prints compact JSON)

| Command | What it does |
| --- | --- |
| `init` | create the database |
| `upsert [file\|-]` | insert/update leads from a JSON object or list (file or stdin) |
| `list [--status S] [--q TEXT] [--full]` | compact lead list |
| `get <id\|url>` | one lead with queue blockers, budget view, events and company applications |
| `queue [--explain]` | qualified leads ready for a person to invite; `--explain` lists blocked ones and why |
| `update-status <id\|url> <status> --evidence TEXT [--evidence-url URL]` | workflow move, logged as a permanent event |
| `upsert-applications [file\|-]` | record past job applications and how a rejection was decided |
| `applications [--revisit]` | recorded applications; `--revisit` = rejections worth reaching via people |
| `export [--format json\|csv] [--out FILE]` | export leads (`--out` refuses to overwrite) |
| `stats`, `runs`, `policy` | counts, run history, current policy |
| `record-run [file\|-]` | log a session: `{"kind","summary","started_at?","finished_at?","counts?":{name:int},"notes?"}` |

Global options: `--db PATH` (or `LNAPP_DB`), `--settings PATH`. Exit codes: 2 bad input, 3 not found.
Default data: `%LOCALAPPDATA%/lead-workbench` on Windows, otherwise
`${XDG_DATA_HOME:-~/.local/share}/lead-workbench`. Override with `LEAD_WORKBENCH_HOME`.
Plugin updates do not move your data. Existing `app/data/` databases are not moved automatically.

Sync commands: `google-connect --client PATH`, `google-disconnect`, `sync-google`,
`sync-conflicts`, `sync-resolve RECORD REVISION`, `delete-record KIND ID`, `restore-record KIND ID`.
See the sync guide before connecting or uploading records.

## Lead fields

`linkedin_url` (required; folded to `https://www.linkedin.com/in/<slug>`: query, fragment,
sub-pages, `uk.`/`ro.`/`m.` hosts and case removed), `name`, `company`, `title`,
`contact_location` (as shown on the profile), `contact_country`, `contact_location_source`,
`company_market`, `company_type` (unknown, product, consultancy, agency, recruitment_agency,
low_cost_staff_augmentation), `hiring_signal` + `hiring_source_url` + `hiring_date`,
`eligibility` (unknown, worldwide, europe_including_romania, us_only, uk_only, other_restricted)
+ `eligibility_source_url`, `budget_min`/`budget_max`/`budget_currency`/`budget_period`
(unknown, hour, day, month, year) + `budget_source_url`, `relationship` (unknown, none, 1st,
2nd, 3rd), `invitation_draft`, `hold_reason`, `exclusion_reason`, `last_checked`, `notes`.
An import may also carry `status` (research, qualified, excluded, uncertain) with
`status_evidence` / `status_evidence_url`.

Rules worth knowing:

- Absent or `null` fields in an import keep what is stored; to clear a field use the UI.
- Unknown field names are an error (catches typos).
- A claim needs its source: a budget number needs currency, period and `budget_source_url`;
  eligibility other than `unknown` needs `eligibility_source_url`; a hiring signal needs
  `hiring_source_url`. Budgets are never converted between currencies or periods, and never
  guessed from a country.
- A batch is one transaction: one bad item and nothing is written.
- Imports never move a lead out of `pending`, `sent`, `connected` or `rejected`; the result says
  `preserved_status` (research facts such as title still update). Invitation states are set
  only with `update-status` and evidence.
- An import that sets or changes `status` needs `status_evidence`, and the move must be one
  `update-status` would allow (so `excluded -> qualified` must go through `research`).
- Numbers must be finite, 0 to 1,000,000,000; dates are `YYYY-MM-DD` or ISO date-time.
- Input and output are UTF-8 on every platform (Windows pipes included).

## Statuses

`research -> qualified -> sent -> pending -> connected`, with `uncertain` and `excluded` for
research, and `rejected` for an invitation declined or withdrawn. Research rejection is
`excluded` (with `exclusion_reason`); a temporary research hold is `hold_reason` and keeps the
lead out of the queue. Allowed moves are in `TRANSITIONS` in `app/core.py`; `connected` is final.

A lead is ready to invite when it is `qualified`, has no hold, its contact country is known and
not excluded, the company verifiably meets the configured hiring eligibility (with a
source link), the company type is not skipped, and there is a draft within the note limit.
The default example accepts worldwide hiring and has no country exclusions or budget goal.

If configured, excluded locations are matched on `contact_country` (country name, codes,
local spellings, major cities) and on whole words in `contact_location`, ignoring accents. The
lists are in `excluded_contact_country_aliases` in your private policy JSON. Only the stated location
is used, never a name.

## Past applications

```json
[{"company": "Acme Ltd", "role_title": "Senior Engineer", "applied_date": "2026-09-01",
  "outcome": "rejected", "rejection_date": "2026-09-02",
  "rejection_source": "automated_email", "decision_basis": "automated_email_only",
  "evidence": "Generic no-reply email two days after applying", "evidence_url": null}]
```

`rejection_source`: unknown, automated_email, ats_status, recruiter_message,
hiring_manager_message. `decision_basis`: unknown, `automated_email_only` (an automated email
says no, but nothing shows who decided), `ats_auto_reject_proven` (evidence the ATS decided alone,
e.g. a knockout question; evidence text required), `human_screening_confirmed` (a person said so).
Keyed by company, role and applied date. Write these records yourself from what you know; the
app does not read mailboxes. Leads are linked to applications by company name (legal suffixes
like Ltd/GmbH ignored).

## Policy

The app starts with the neutral `app/settings.example.json`. Copy it to `settings.json`
in your private data directory to customize preferred markets, hiring eligibility, optional location filters,
company types, a budget goal, and the invitation note limit. Restart after editing. You can also
pass `--settings PATH` before the command. Legacy `app/settings.json` is still supported. The existing eligibility vocabulary includes
`europe_including_romania`; select only values whose meaning matches your situation.

## Source layout

`app/core.py` contains storage and workflow rules; `app/cli.py` provides the JSON CLI;
`app/server.py` exposes the local HTTP API; `app/mcp_server.py` exposes the plugin tools. The frontend is in `app/static/` and calls that API.
The frontend and API are separate source files served by one Python process.
There is no hosted service, AI provider dependency, or automatic LinkedIn integration.
`app/sync.py` handles record revisions and conflicts; `app/google_drive.py` supplies optional Drive transport.

## Data and examples

This repository contains source code, tests, and fictional demo records only. Actual contacts,
applications, invitation notes, databases, exports, logs, screenshots, and personal settings
belong outside Git. `.gitignore` excludes common local data paths; review every staged change.
The database is local and unencrypted. The loopback server is intended for personal use on a
trusted machine, not public deployment.

To try the fictional demo in its own ignored database:

```sh
python -m app --db local/demo.sqlite3 upsert examples/leads.json
python -m app --db local/demo.sqlite3 upsert-applications examples/applications.json
python -m app --db local/demo.sqlite3 serve
```

Demo profile URLs are synthetic placeholders. No invitations or applications are submitted.
An automated rejection email is recorded as `automated_email_only`, without claiming that
an ATS made the decision.

## License

MIT - see [LICENSE](LICENSE).

## Security

Binds 127.0.0.1 only. Mutations need a JSON body, the per-process token in `X-CSRF-Token`, a
`Host` of `127.0.0.1`/`localhost` on the server's port, and a same-origin `Origin` /
`Sec-Fetch-Site` when the browser sends them. Strict CSP (`'self'` only), all UI text set via
`textContent`, links limited to http(s). Events are immutable (SQLite triggers). CSV export
prefixes cells starting with `= + - @`.

## Tests

```
python -m unittest discover -s tests -t . -v
```

For real client installation/startup checks in isolated profiles (both clients must be installed):

```sh
python scripts/package_plugin.py --out local/validation-package
python scripts/validate_plugins.py local/validation-package
```

The client check starts MCP connections without running an AI turn or connecting Google.
See [validation scope](docs/VALIDATION.md) for what has and has not been verified.
