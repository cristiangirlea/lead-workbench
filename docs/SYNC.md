# Optional Google Drive sync

Local mode is the default. Google Drive is an optional shared store for the same user's
devices, not a database service operated by this project. Both laptops need the same Google
account and OAuth application. They do not need to be online at the same time.
Sync must remain within free provider usage; do not link Cloud billing or buy quota/storage
to make it work. Quota exhaustion stops sync while preserving local changes. See the
[free-service policy](FREE_SERVICE.md).

## Google setup (operator action)

1. In your Google Cloud project, enable the Google Drive API.
2. Configure the OAuth consent screen for your intended audience. For a test application,
   add your Google account as a test user when required by Google.
3. Create an OAuth client of type **Desktop app**, supporting the loopback redirect flow.
4. Download its client JSON and keep it outside the repository. Use the same registered
   Desktop client on both laptops so the app-data store belongs to the same application.
5. Before distributing to other users, complete the applicable Google OAuth publishing,
   branding and verification steps. This project does not create a Cloud project, accept
   agreements, or complete verification for you.

The only requested scope is `https://www.googleapis.com/auth/drive.appdata`. It accesses the
app's hidden application data folder. It does not grant general Drive access, Gmail access,
or access to other users' data. The records uploaded contain your contacts, notes, application
facts and audit history; review those records before opting in.

## Connect each laptop

From the source or installed package directory, with the intended data-directory environment:

```sh
python -m app google-connect --client /private/path/desktop-client.json
```

This opens Google's browser consent screen. The local callback checks a random state value
and uses PKCE. Cancellation leaves local mode available. Connecting saves credentials but
does not upload records. On Windows, refresh credentials use Windows DPAPI for the current
user; on POSIX, they use an owner-only file. Connect separately on every laptop rather than
copying token files. Do not paste credentials into an assistant conversation.

## Sync

```sh
python -m app sync-google
```

Or explicitly ask the assistant to use `sync_google`. Sync is manual/on request in this
version: there is no background service and no automatic upload just because you install
the plugin or connect Google. Sync on laptop A before closing it, and on laptop B before
continuing. Changes saved while offline persist locally and upload when you retry after
reconnecting. A powered-off laptop's unsynced changes cannot appear elsewhere until it runs
sync. Changes made during network requests are retained and may need another sync pass;
inspect `pending_upload` and repeat until zero.

Sync returns uploaded/downloaded counts, pending local uploads and unresolved conflicts.
Failure is reported as an error; it is never a successful-sync timestamp. Reconnect if
authorization expires or is revoked. Retries on temporary network failures and rate limits
are bounded; rerun later if they fail.

## Conflict handling

The sync layer stores immutable, content-addressed revisions with parent revisions. Device
clocks do not choose a winner. A newer edit that observed the old version supersedes it;
concurrent edits create a conflict. Whole-record conflicts are explicit, even when different
fields were edited. Repeated delivery is deduplicated by revision ID. Leads use canonical
profile URLs; applications use normalized company, role and applied date as their identity.

```sh
python -m app sync-conflicts
python -m app sync-resolve 'RECORD_KEY_FROM_OUTPUT' REVISION_ID_FROM_OUTPUT
python -m app sync-google
```

Inspect both values before choosing. Resolution creates a revision descending from all
conflicting versions. Old versions remain in history. A new laptop receives a provisional
visible version plus the conflict; it does not silently resolve it. Conflicted leads are
blocked from the ready-to-invite queue. Sync refuses to overwrite a further local edit to
a conflicted record; inspect and preserve that edit before explicitly choosing a revision.

## Deletions and restoration

```sh
python -m app delete-record lead 1
python -m app delete-record application 1
python -m app sync-google
python -m app restore-record lead 1
```

IDs are local to each database; look them up on the laptop where you run the command.
Deletion is a tombstone: it hides the record from normal views, exports and queues and syncs
that state. Imports do not silently restore it. A concurrent deletion/edit requires a choice.
Restoration is explicit. Tombstones, past revisions and audit events remain stored; this is
**not permanent privacy erasure**. To erase the dataset permanently, remove the app's stored
data through Google Account/Drive controls and remove private local databases/backups on all
devices. Do not reconnect an old database to a cleared store unless you intend to upload it.

## Disconnect and limits

```sh
python -m app google-disconnect
```

This deletes credentials on this laptop. Revoke the app under Google Account permissions
to revoke provider access. Neither action automatically erases local records or remote data.
Do not share a data directory between people or reuse it for a different Google account
unless you deliberately intend to upload those records to the new account.

The app synchronizes structured JSON revision files, never the live SQLite file or its WAL.
Record values and retained history are readable by the authorized app and Google storage;
this version does not provide end-to-end encryption. Credentials, settings and local database
IDs are not synced. Audit event references are remapped to each device's local IDs.

This initial implementation lists retained revisions each sync and downloads their contents.
It favors correctness and recovery over large-dataset efficiency; history compaction and
incremental download caching are future improvements. Do not edit app-data files manually.
There is no team sharing or Gmail import in this release. See [ROADMAP.md](ROADMAP.md).

## Validation boundaries

Automated tests run two/three independent databases and the real HTTP client against a
local Drive-protocol fixture, plus desktop OAuth callback/state/PKCE tests with fictional
tokens. These do not establish Google's approval or production quotas. A separate live
roundtrip is recorded in [VALIDATION.md](VALIDATION.md). Complete a live opt-in test with your registered OAuth client before
relying on Google sync for your only copy of important records.

Sources:
- https://developers.google.com/workspace/drive/api/guides/appdata
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
