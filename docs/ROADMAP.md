# Roadmap

## Current goal

- Installable Codex and Claude Code plugins sharing the same local workbench backend.
- Local storage without an account; optional Google Drive app-data sync across devices.
- Durable offline changes, retries, duplicate prevention, explicit conflict resolution and deletions.
- Fictional fixtures, tests and setup documentation; no personal data or credentials in Git.

## Future: optional Gmail application import

Not part of the current plugin/sync release. No Gmail access is requested by Drive sync.

- Separate opt-in and Google consent for Gmail read-only access; never request send, modify or delete permissions.
- Let the user choose a date range and optionally a Gmail label; explain that Gmail's OAuth permission itself is broader than the app's search filter.
- Find application confirmations, interview invitations and status updates. Email coverage is incomplete: absence of a message does not mean the user never applied.
- Show proposed company, role, application date, outcome, supporting message reference and uncertainty for review before import.
- Deduplicate by account/message ID and application identity; preserve manually verified facts and flag ambiguous matches.
- An automated rejection email is not proof an ATS made the rejection decision.
- Save minimal structured evidence; do not copy full messages or attachments into sync by default. Explicitly disclose any message content sent to an AI provider.
- Support incremental refresh, revoked access, disconnect and deletion of imported data.
- Gmail read-only is a restricted Google scope. Determine applicable verification and any security assessment requirements before public release, especially if derived mailbox data is transmitted or stored remotely.

Sources:
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/workspace/drive/api/guides/appdata
