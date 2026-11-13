# Validation scope for 0.2.0

Validated on Windows with Python 3.13.7, Codex CLI 0.167.0-alpha.4.1 and Claude Code 2.1.284.
Python 3.10 is the intended minimum; other Python/OS combinations have not been run here.

## Commands

```sh
python -m unittest discover -s tests -t . -v
node --check app/static/app.js
python scripts/package_plugin.py --out local/validation-package
python scripts/validate_plugins.py local/validation-package
claude plugin validate local/validation-package/.claude-plugin/plugin.json
claude plugin validate local/validation-package/.claude-plugin/marketplace.json
git diff --check
```

The automated suite covers 78 tests, including the original CLI/UI behavior plus:

- Independent laptop databases, restart after upload, offline changes and idempotent retry
  after a lost upload response.
- Concurrent creation/edit/delete, explicit resolution, a third laptop downloading an
  unresolved conflict, and preservation of unrelated unsynced edits during resolution.
- Edits made during network IO, tombstones/restoration, hidden deleted records, blocked
  conflicted invitation queues, invalid remote hashes, and missing-parent recovery.
- Audit-history reference mapping across different local IDs and run-history deduplication.
- MCP initialization/discovery/calls from unrelated working directories; separate client
  processes reading shared local records; malformed tool arguments and offline operation.
- Drive multipart upload/download, pagination, transient-error retries and denied access,
  using the real urllib HTTP client against an isolated local protocol fixture.
- Browser callback state/PKCE and requested scope using fictional OAuth responses;
  credential roundtrip with real Windows DPAPI; cancelled setup without saved credentials.
- Allowlisted package contents excluding private settings, database files and runtime data.

The isolated real-client check installs the clean package into separate temporary profiles.
Codex app-server initializes the bundled MCP server and discovers 14 tools. Claude Code's
MCP health command reports the plugin connected. No LLM turn or normal user profile is used.

## Live Google Drive check

On 2026-11-13, a dedicated Desktop OAuth app connected one consenting account using only
`drive.appdata`. The Google Cloud project had no linked billing account. Two independent
local SQLite databases (not two physical laptops) exchanged one explicitly fictional lead:
upload on A, download on B, update on B, receive on A, idempotent repeat, and deletion on
both. Every completed pass reported zero pending uploads and zero conflicts. No real
contacts were uploaded. The fictional lead is hidden by a synced tombstone; its fictional
audit revisions remain in the app-data store.

Codex installation was also verified in the normal user profile. Public source contains no
OAuth client downloads, refresh credentials, real contact data or private execution proof.

## External steps not performed

- Additional laptops/accounts still need their own Google consent. See [SYNC.md](SYNC.md).
- No public plugin-directory listing, Google verification, publisher approval or provider
  agreement has been submitted. Local installation does not imply public acceptance.
- Gmail import is documented as a future opt-in feature, not implemented or authorized.
- No existing personal workbench database was migrated or connected to the new plugins.

Local/protocol-fixture tests, the live account roundtrip, and public directory approval are
separate validation scopes. The live roundtrip does not establish unlimited free capacity
or public OAuth/plugin-directory approval.
