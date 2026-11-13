"""Content-addressed immutable revisions with explicit concurrent-edit conflicts.

The local database is authoritative while offline. Capture compares persisted rows
against the last materialized baseline inside a write transaction. It runs again
after network IO so an edit made during IO cannot be overwritten by a download.
No timestamps or device clocks are used to decide which edit wins.
"""
import hashlib
import json
import uuid

from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_ops (
    id TEXT PRIMARY KEY, record TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_ops_record ON sync_ops(record);
CREATE TABLE IF NOT EXISTS sync_baseline (record TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_uploaded (remote TEXT NOT NULL, id TEXT NOT NULL,
    PRIMARY KEY (remote, id));
"""
TABLES = ("leads", "applications", "events", "runs")
MAX_OPERATION_BYTES = 100_000


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf8")).hexdigest()


def install(conn):
    conn.executescript(SCHEMA)


def lead_key(row):
    return "lead:" + row["linkedin_url"]


def application_key(row):
    return "application:" + encode([row["company_key"], row["role_title"], row["applied_date"]])


def snapshot(conn):
    records, leads, applications = {}, {}, {}
    for table, key_fn, mapping in (("leads", lead_key, leads), ("applications", application_key, applications)):
        for row in conn.execute(f"SELECT * FROM {table}"):
            value = dict(row)
            local_id = value.pop("id")
            value.pop("sync_conflicted", None)  # Derived device-local state, never a revision.
            key = key_fn(value)
            records[key] = value
            mapping[local_id] = key
    for table in ("events", "runs"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            value = dict(row)
            value.pop("id")
            if table == "events":
                value["lead_ref"] = leads.get(value.pop("lead_id"))
                value["application_ref"] = applications.get(value.pop("application_id"))
            records[table + ":" + digest(value)] = value
    return records


def all_ops(conn):
    return {r["id"]: json.loads(r["body"]) for r in conn.execute("SELECT id, body FROM sync_ops")}


def record_heads(ops, key):
    candidates = {i for i, op in ops.items() if op["record"] == key}
    ancestors = {p for i in candidates for p in ops[i]["parents"]}
    return sorted(candidates - ancestors)


def distinct_values(ops, heads):
    return {encode(ops[i]["value"]) for i in heads}


def insert_revision(conn, key, value, parents):
    revision = {"schema": 1, "record": key, "value": value,
                "parents": sorted(parents), "nonce": uuid.uuid4().hex}
    ident = digest(revision)
    revision["id"] = ident
    conn.execute("INSERT OR IGNORE INTO sync_ops VALUES (?, ?, ?)", (ident, key, encode(revision)))
    return ident


def set_baseline(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO sync_baseline VALUES (?, ?)", (key, encode(value)))


def capture(conn):
    ops = all_ops(conn)
    before = {r["record"]: r["body"] for r in conn.execute("SELECT * FROM sync_baseline")}
    added = 0
    for key, value in snapshot(conn).items():
        if before.get(key) == encode(value):
            continue
        heads = record_heads(ops, key)
        if len(distinct_values(ops, heads)) > 1:
            raise core.ValidationError("local edit to conflicted record; resolve the conflict before syncing: " + key)
        insert_revision(conn, key, value, heads)
        set_baseline(conn, key, value)
        added += 1
    return added


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")} - {"id", "sync_conflicted"}


def validate_revision(conn, revision):
    if not isinstance(revision, dict) or set(revision) != {"id", "schema", "record", "value", "parents", "nonce"}:
        raise core.ValidationError("invalid sync revision fields")
    if revision["schema"] != 1 or type(revision["schema"]) is not int:
        raise core.ValidationError("unsupported sync schema")
    unsigned = {k: v for k, v in revision.items() if k != "id"}
    if len(encode(revision).encode("utf8")) > MAX_OPERATION_BYTES or revision["id"] != digest(unsigned):
        raise core.ValidationError("invalid sync revision hash or size")
    key, value, parents = revision["record"], revision["value"], revision["parents"]
    if not isinstance(key, str) or not isinstance(value, dict) or not isinstance(parents, list):
        raise core.ValidationError("invalid sync record")
    if len(parents) > 1000 or any(not isinstance(p, str) or len(p) != 64 for p in parents):
        raise core.ValidationError("invalid revision parents")
    if not isinstance(revision["nonce"], str) or len(revision["nonce"]) != 32:
        raise core.ValidationError("invalid revision nonce")
    if key.startswith("lead:"):
        table = "leads"
        fields = {k: v for k, v in value.items() if k in core.LEAD_FIELDS}
        core.clean_lead_fields(fields)
        if value.get("status") not in core.STATUSES:
            raise core.ValidationError("invalid synced lead status")
        if lead_key(value) != key or core.canonical_linkedin_url(value.get("linkedin_url")) != value["linkedin_url"]:
            raise core.ValidationError("invalid synced lead key")
        if (value.get("company_key") or "") != core.company_key(value.get("company")):
            raise core.ValidationError("invalid company key")
        # Do not validate against this laptop's local invitation length policy:
        # a policy change must block the queue, not prevent data recovery.
        core.check_lead_record(value, {"invitation": {"max_note_chars": 2000}})
    elif key.startswith("application:"):
        table = "applications"
        core.clean_application({k: v for k, v in value.items() if k in core.APP_FIELDS})
        core.check_application(value)
        if application_key(value) != key or value.get("company_key") != core.company_key(value.get("company")):
            raise core.ValidationError("invalid application key")
    elif key.startswith("events:") or key.startswith("runs:"):
        table = key.split(":", 1)[0]
        if key != table + ":" + digest(value) or parents:
            raise core.ValidationError("history must be immutable")
    else:
        raise core.ValidationError("invalid record kind")
    columns = _columns(conn, table)
    if table == "events":
        columns = columns - {"lead_id", "application_id"} | {"lead_ref", "application_ref"}
    if set(value) != columns:
        raise core.ValidationError("invalid sync payload columns")
    if table in ("leads", "applications") and (type(value["sync_deleted"]) is not int or value["sync_deleted"] not in (0, 1)):
        raise core.ValidationError("invalid tombstone")
    for item in value.values():
        if item is not None and type(item) not in (str, int, float):
            raise core.ValidationError("sync values must be scalar")


def ingest(conn, revisions):
    """Validate the complete union before writing anything. Missing parents retry later."""
    ops = all_ops(conn)
    for revision in revisions:
        validate_revision(conn, revision)
        ident = revision["id"]
        if ident in ops and encode(ops[ident]) != encode(revision):
            raise core.ValidationError("revision ID collision")
        ops[ident] = revision
    # Parent links must be same-record, present and acyclic. Hash-addressing makes
    # cycles implausible, but explicit validation also bounds malformed imports.
    children = {ident: [] for ident in ops}
    indegree = {ident: len(set(op["parents"])) for ident, op in ops.items()}
    for ident in ops:
        for parent in set(ops[ident]["parents"]):
            if parent not in ops:
                raise core.ValidationError("incomplete sync history; retry after the other device finishes uploading")
            if ops[parent]["record"] != ops[ident]["record"]:
                raise core.ValidationError("cross-record revision parent")
            children[parent].append(ident)
    ready = [i for i, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        ident = ready.pop()
        visited += 1
        for child in children[ident]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(ops):
        raise core.ValidationError("cyclic revision graph")
    for ident, revision in ops.items():
        conn.execute("INSERT OR IGNORE INTO sync_ops VALUES (?, ?, ?)", (ident, revision["record"], encode(revision)))
    materialize(conn, ops)


def materialize(conn, ops):
    current = snapshot(conn)
    keys = {op["record"] for op in ops.values()}
    # Base records first, so history references can be mapped to device-local IDs.
    for key in sorted(keys, key=lambda k: (not k.startswith(("lead:", "application:")), k)):
        heads = record_heads(ops, key)
        if len(distinct_values(ops, heads)) != 1:
            if key not in current:
                # A new laptop still needs a provisional row for audit-history
                # foreign keys. Keep the conflict explicit; this is not resolution.
                provisional = ops[heads[0]]["value"]
                _write_record(conn, key, provisional)
                set_baseline(conn, key, provisional)
            _flag_conflict(conn, key, ops[heads[0]]["value"], True)
            continue  # Explicitly unresolved. Keep this laptop's visible row intact.
        value = ops[heads[0]]["value"]
        if current.get(key) != value:
            _write_record(conn, key, value)
        _flag_conflict(conn, key, value, False)
        set_baseline(conn, key, value)


def _flag_conflict(conn, key, value, conflicted):
    if key.startswith("lead:"):
        conn.execute("UPDATE leads SET sync_conflicted=? WHERE linkedin_url=?", (int(conflicted), value["linkedin_url"]))
    elif key.startswith("application:"):
        conn.execute("UPDATE applications SET sync_conflicted=? WHERE company_key=? AND role_title=? AND applied_date=?",
                     (int(conflicted), value["company_key"], value["role_title"], value["applied_date"]))


def _write_record(conn, key, value):
    value = dict(value)
    if key.startswith("lead:"):
        table, where, args = "leads", "linkedin_url = ?", (value["linkedin_url"],)
    elif key.startswith("application:"):
        table, where, args = "applications", "company_key = ? AND role_title = ? AND applied_date = ?", (
            value["company_key"], value["role_title"], value["applied_date"])
    elif key.startswith("events:"):
        for kind, table_name, key_fn in (("lead", "leads", lead_key), ("application", "applications", application_key)):
            ref = value.pop(kind + "_ref")
            found = next((r["id"] for r in conn.execute(f"SELECT * FROM {table_name}") if key_fn(r) == ref), None)
            if ref is not None and found is None:
                raise core.ValidationError("missing history reference; retry sync")
            value[kind + "_id"] = found
        table, where, args = "events", None, None
    else:
        table, where, args = "runs", None, None
    # Table and column names are fixed and validated, never remote SQL identifiers.
    existing = conn.execute(f"SELECT id FROM {table} WHERE {where}", args).fetchone() if where else None
    if existing:
        conn.execute(f"UPDATE {table} SET " + ",".join(k + "=?" for k in value) + " WHERE id=?",
                     list(value.values()) + [existing["id"]])
    else:
        conn.execute(f"INSERT INTO {table} ({','.join(value)}) VALUES ({','.join('?' for _ in value)})", list(value.values()))


def conflicts(conn):
    install(conn)
    ops = all_ops(conn)
    result = []
    for key in sorted({op["record"] for op in ops.values()}):
        heads = record_heads(ops, key)
        if len(distinct_values(ops, heads)) > 1:
            result.append({"record": key, "versions": [{"revision": i, "value": ops[i]["value"]} for i in heads]})
    return result


def resolve(conn, key, revision):
    install(conn)
    with core._tx(conn):
        ops = all_ops(conn)
        heads = record_heads(ops, key)
        if revision not in heads or len(distinct_values(ops, heads)) < 2:
            raise core.ValidationError("choose a current revision from an unresolved conflict")
        value = ops[revision]["value"]
        # Resolution is a new revision descending from every conflicting head.
        ident = insert_revision(conn, key, value, heads)
        # Resolve this record only. Other records may contain unpublished local
        # changes, so materializing the entire store here would overwrite them.
        _write_record(conn, key, value)
        _flag_conflict(conn, key, value, False)
        set_baseline(conn, key, value)
    return {"resolved": key, "revision": ident}


def set_deleted(conn, kind, ref, deleted=True):
    """Soft deletion syncs; audit history and tombstones remain to prevent resurrection."""
    install(conn)
    if kind not in ("lead", "application"):
        raise core.ValidationError("kind must be lead or application")
    table = "leads" if kind == "lead" else "applications"
    with core._tx(conn):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (int(ref),)).fetchone()
        if row is None:
            raise core.NotFound("record not found")
        conn.execute(f"UPDATE {table} SET sync_deleted=?, updated_at=? WHERE id=?", (int(deleted), core.now_iso(), int(ref)))
        capture(conn)
    return {"kind": kind, "id": int(ref), "deleted": deleted}


def synchronize(conn, remote):
    """Exchange durable operations. Transport errors leave local work retryable."""
    install(conn)
    with core._tx(conn):
        capture(conn)
    uploaded = 0
    acknowledged = {r[0] for r in conn.execute("SELECT id FROM sync_uploaded WHERE remote=?", (remote.identity,))}
    for ident, revision in all_ops(conn).items():
        if ident in acknowledged:
            continue
        remote.put(ident, revision)
        conn.execute("INSERT OR IGNORE INTO sync_uploaded VALUES (?,?)", (remote.identity, ident))
        uploaded += 1
    downloaded = remote.list_operations()
    with core._tx(conn):
        capture(conn)  # Include edits made by UI/another agent during network requests.
        ingest(conn, downloaded)
    # Downloaded revisions are already present remotely and need no echo upload.
    for revision in downloaded:
        conn.execute("INSERT OR IGNORE INTO sync_uploaded VALUES (?,?)", (remote.identity, revision["id"]))
    pending = conn.execute("SELECT count(*) FROM sync_ops WHERE id NOT IN (SELECT id FROM sync_uploaded WHERE remote=?)", (remote.identity,)).fetchone()[0]
    return {"uploaded": uploaded, "downloaded": len(downloaded), "pending_upload": pending,
            "conflicts": conflicts(conn), "synced_at": core.now_iso()}
