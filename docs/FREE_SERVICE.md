# Free distribution and storage policy

Lead Workbench is free, MIT-licensed software. Local use requires no account, subscription,
AI API key, or service operated by this project. The assistant host (Codex or Claude Code)
has its own availability and account requirements; the plugin does not provide free AI usage.

Optional Google Drive sync must remain within Google's free standard API usage and the
user's existing available Drive storage. This project must not require a paid backend,
paid API quota, or a storage subscription to use the workbench.

Operational rules:

- Keep the sync OAuth project's Google Cloud billing account unlinked.
- Do not enable billing, purchase storage, or request paid quota increases as a fix for sync errors.
- If free storage or API quotas are exhausted, report that sync is unavailable and retain
  local changes for later retry. Local use continues without Google.
- If Google requires payment for the supported usage, stop offering that sync route under
  this policy; retain local use/export rather than silently introduce charges.
- Do not promise unlimited or permanently free third-party API capacity. Provider terms
  can change and one shared OAuth project has quotas across its users.
- Public release still needs the applicable OAuth configuration and verification. Free
  distribution does not waive Google's requirements.

Checked 2026-11-13: Google's Drive API documentation states that standard use has no
additional API charge within its quotas and describes potential charges above standard
thresholds. The workbench must not opt into paid usage. App-data files consume Drive storage.

Sources:
- https://developers.google.com/workspace/drive/api/guides/limits
- https://developers.google.com/workspace/tools-safety
- https://developers.google.com/workspace/drive/api/guides/appdata
