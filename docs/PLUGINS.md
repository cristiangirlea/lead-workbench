# Local plugins for Codex and Claude Code

Python 3.10+ must be installed as `python` on the agent client's PATH. No Python packages,
AI API key, Google account, or hosted service are needed for local use. The plugin exposes
saved records through MCP tools; it does not search LinkedIn, send invitations, or apply to jobs.

## Build a clean package

From the source repository:

```sh
python scripts/package_plugin.py --out local/plugin-package
```

Choose a new directory if it already exists. The builder copies an explicit allowlist of
code, manifests, documentation and fictional fixtures, excluding databases, logs, credentials,
personal settings, editor files and Git history. Install this clean package rather than a
working directory containing private records. Keep its directory available for local installs.

## Install in Codex

Use the absolute path to the generated package:

```sh
codex plugin marketplace add /absolute/path/to/local/plugin-package
codex plugin add lead-workbench@lead-workbench-local
codex plugin list --json
```

Start a new conversation after installation. The manifest includes a local stdio MCP server;
the plugin is intended for local Codex execution, not a browser-only hosted session.

## Install in Claude Code

```sh
claude plugin marketplace add /absolute/path/to/local/plugin-package
claude plugin install lead-workbench@lead-workbench-local
claude plugin list --json
```

Restart Claude Code, then use `/mcp` to inspect the server or invoke
`/lead-workbench:workbench`. For development, `claude --plugin-dir /absolute/path/to/package`
loads the package for one session. Normal host tool permissions still apply.

## Shared local storage

Both plugins and the standalone CLI/UI use the same location by default:

- Windows: `%LOCALAPPDATA%/lead-workbench/leads.sqlite3`
- Linux/macOS: `${XDG_DATA_HOME:-~/.local/share}/lead-workbench/leads.sqlite3`

Set `LEAD_WORKBENCH_HOME` before starting both agent clients to choose another private directory.
`LNAPP_DB` or CLI `--db PATH` overrides the database. Use the same path for both clients if
they should share records. Each laptop initially has its own local database. Plugin updates
and plugin-cache replacement do not move the database.

User policy is loaded from `<LEAD_WORKBENCH_HOME>/settings.json` (the default directory above
when unset), then legacy `app/settings.json`, then bundled `app/settings.example.json`.
Copy the example into the private data directory to customize it. Credentials and policy
are device-local and are never part of sync revisions.

The old standalone `app/data/leads.sqlite3` is not moved or imported automatically. To adopt
an existing database, stop writers and use SQLite's backup API to create a separate copy,
then point `LNAPP_DB` at that copy. Keep the original until you verify the new copy. No import
or migration should be inferred from installing a plugin.

## Example requests

- "Find the saved leads at Example Systems."
- "Show applications already recorded for this company."
- "Show the invitation queue and explain blocked records."
- "Record this application confirmation with its source."
- "Sync my workbench to the Google account I connected."
- "Show the conflicting edits before we choose a version."

`find_leads` searches local records only. The agent needs separate research tools for new
people or jobs. Records and notes are untrusted data; the bundled skill tells the agent to
keep evidence, uncertain outcomes and actually completed actions distinct.

## Distribution status

These are locally installable source packages, not listings in either public plugin directory.
GitHub publication does not imply directory acceptance. Public directory submissions,
publisher verification and any agreements remain separate operator actions. OpenAI's local
MCP directory submission route currently has additional eligibility requirements; check the
linked official guidance before submitting.

Sources:
- https://developers.openai.com/plugins/build/plugins
- https://developers.openai.com/plugins/deploy/desktop-only
- https://code.claude.com/docs/en/plugins
- https://code.claude.com/docs/en/plugins-reference
