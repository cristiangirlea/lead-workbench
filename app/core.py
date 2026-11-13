"""Storage, validation and workflow rules for the local lead workbench.

Everything here is local: a SQLite file, no network access. The app records what a
person found and did on LinkedIn; it never sends invitations or fetches pages.
"""

import json
import math
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote, urlsplit
from .paths import data_dir

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = data_dir() / "leads.sqlite3"
SETTINGS_PATH = APP_DIR / "settings.json"

STATUSES = ("research", "qualified", "pending", "sent", "connected", "rejected", "excluded", "uncertain")
# Invitation states. A research import never changes a lead out of these.
PROTECTED_STATUSES = frozenset({"pending", "sent", "connected", "rejected"})
# Statuses a research import may set; the rest need update-status with evidence.
RESEARCH_STATUSES = frozenset({"research", "qualified", "excluded", "uncertain"})
TRANSITIONS = {
    "research": {"qualified", "excluded", "uncertain"},
    "uncertain": {"research", "qualified", "excluded"},
    "qualified": {"research", "uncertain", "excluded", "sent", "pending"},
    "sent": {"pending", "connected", "rejected"},
    "pending": {"connected", "rejected"},
    "rejected": {"research"},
    "excluded": {"research"},
    "connected": set(),
}

ELIGIBILITY = ("unknown", "worldwide", "europe_including_romania", "us_only", "uk_only", "other_restricted")
COMPANY_TYPES = ("unknown", "product", "consultancy", "agency", "recruitment_agency", "low_cost_staff_augmentation")
RELATIONSHIPS = ("unknown", "none", "1st", "2nd", "3rd")
BUDGET_PERIODS = ("unknown", "hour", "day", "month", "year")

APP_OUTCOMES = ("unknown", "applied", "interviewing", "rejected", "withdrawn", "offer")
REJECTION_SOURCES = ("unknown", "automated_email", "ats_status", "recruiter_message", "hiring_manager_message")
DECISION_BASES = ("unknown", "automated_email_only", "ats_auto_reject_proven", "human_screening_confirmed")
REVISIT_STATUSES = ("todo", "researching", "contacts_found", "done", "skip")

TEXT_LIMITS = {
    "name": 200, "company": 200, "title": 300, "contact_location": 200, "contact_country": 100,
    "contact_location_source": 500, "company_market": 100, "hiring_signal": 1000, "hold_reason": 500,
    "exclusion_reason": 500, "invitation_draft": 2000, "notes": 5000, "budget_currency": 3,
}
URL_FIELDS = ("hiring_source_url", "eligibility_source_url", "budget_source_url")
DATE_FIELDS = ("hiring_date", "last_checked")
ENUM_FIELDS = {
    "eligibility": ELIGIBILITY, "company_type": COMPANY_TYPES,
    "relationship": RELATIONSHIPS, "budget_period": BUDGET_PERIODS,
}
NUMBER_FIELDS = ("budget_min", "budget_max")
MAX_NUMBER = 10**9
MAX_ID = 2**62
LEAD_FIELDS = (
    ("linkedin_url",) + tuple(TEXT_LIMITS) + URL_FIELDS + DATE_FIELDS + tuple(ENUM_FIELDS) + NUMBER_FIELDS
)
# Accepted in an import payload but not stored as columns.
UPSERT_EXTRA = ("status", "status_evidence", "status_evidence_url")

APP_TEXT_LIMITS = {"company": 200, "role_title": 300, "evidence": 2000, "notes": 5000}
APP_FIELDS = tuple(APP_TEXT_LIMITS) + (
    "applied_date", "rejection_date", "outcome", "rejection_source", "decision_basis",
    "evidence_url", "revisit_status",
)
APP_ENUMS = {
    "outcome": APP_OUTCOMES, "rejection_source": REJECTION_SOURCES,
    "decision_basis": DECISION_BASES, "revisit_status": REVISIT_STATUSES,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    linkedin_url TEXT NOT NULL UNIQUE,
    name TEXT, company TEXT, company_key TEXT, title TEXT,
    contact_location TEXT, contact_country TEXT, contact_location_source TEXT,
    company_market TEXT, company_type TEXT NOT NULL DEFAULT 'unknown',
    hiring_signal TEXT, hiring_source_url TEXT, hiring_date TEXT,
    eligibility TEXT NOT NULL DEFAULT 'unknown', eligibility_source_url TEXT,
    budget_min REAL, budget_max REAL, budget_currency TEXT,
    budget_period TEXT NOT NULL DEFAULT 'unknown', budget_source_url TEXT,
    relationship TEXT NOT NULL DEFAULT 'unknown',
    invitation_draft TEXT,
    status TEXT NOT NULL DEFAULT 'research',
    hold_reason TEXT, exclusion_reason TEXT,
    last_checked TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS leads_company_key ON leads(company_key);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    application_id INTEGER REFERENCES applications(id),
    kind TEXT NOT NULL,
    from_status TEXT, to_status TEXT,
    evidence TEXT, evidence_url TEXT,
    detail TEXT,
    actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_lead ON events(lead_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL, company_key TEXT NOT NULL,
    role_title TEXT NOT NULL DEFAULT '',
    applied_date TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'unknown',
    rejection_date TEXT,
    rejection_source TEXT NOT NULL DEFAULT 'unknown',
    decision_basis TEXT NOT NULL DEFAULT 'unknown',
    evidence TEXT, evidence_url TEXT,
    revisit_status TEXT NOT NULL DEFAULT 'todo',
    notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (company_key, role_title, applied_date)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT, finished_at TEXT,
    summary TEXT NOT NULL,
    counts TEXT, notes TEXT,
    recorded_at TEXT NOT NULL
);
"""


class ValidationError(ValueError):
    """Bad input; the message says which field and why."""


class NotFound(LookupError):
    pass


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_settings(path=None):
    candidates = [data_dir() / "settings.json", SETTINGS_PATH, APP_DIR / "settings.example.json"]
    selected = Path(path) if path else next(p for p in candidates if p.exists())
    with open(selected, encoding="utf-8") as fh:
        return json.load(fh)


def connect(db_path=None):
    path = Path(db_path or os.environ.get("LNAPP_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # Additive migration; existing personal databases remain readable and intact.
    with _tx(conn):
        for table in ("leads", "applications"):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "sync_deleted" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_deleted INTEGER NOT NULL DEFAULT 0")
            if "sync_conflicted" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_conflicted INTEGER NOT NULL DEFAULT 0")
    return conn


class _tx:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any error: a batch lands whole or not at all."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# ---------------------------------------------------------------- normalising

_LINKEDIN_HOST = re.compile(r"^(?:[a-z]{2,3}\.|www\.|m\.)?linkedin\.com$")


def canonical_linkedin_url(raw):
    """Return https://www.linkedin.com/in/<slug> or raise ValidationError.

    Strips query, fragment, trailing slashes and sub-pages; folds country hosts
    (uk., ro., m.) and case so one person has one key.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("linkedin_url: required")
    text = raw.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        has_extra = parts.username or parts.password or parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ValidationError("linkedin_url: not a valid address") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValidationError("linkedin_url: must be http(s)")
    if has_extra:
        raise ValidationError("linkedin_url: must not carry credentials or a port")
    if not _LINKEDIN_HOST.match(host):
        raise ValidationError("linkedin_url: not a linkedin.com address")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() != "in":
        raise ValidationError("linkedin_url: must be a personal profile (/in/<name>)")
    slug = unquote(segments[1]).strip().lower()
    if not slug or len(slug) > 200 or any(c in slug for c in "/?#\\ ") or any(ord(c) < 32 for c in slug):
        raise ValidationError("linkedin_url: profile name is not valid")
    return "https://www.linkedin.com/in/" + quote(slug, safe="-_.~")


def check_http_url(field, value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValidationError(f"{field}: must be a URL string under 2000 characters")
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
        parts.port
    except ValueError:
        raise ValidationError(f"{field}: not a valid link") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValidationError(f"{field}: must be an http(s) link")
    if parts.username or parts.password:
        raise ValidationError(f"{field}: must not carry credentials")
    if any(ord(c) < 33 for c in value.strip()):
        raise ValidationError(f"{field}: must not contain spaces or control characters")
    return value.strip()


def check_date(field, value):
    if not isinstance(value, str):
        raise ValidationError(f"{field}: must be a YYYY-MM-DD or ISO date-time string")
    v = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?", v):
        raise ValidationError(f"{field}: must be a YYYY-MM-DD or ISO date-time string")
    try:
        if len(v) == 10:
            date.fromisoformat(v)
        else:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{field}: must be a YYYY-MM-DD or ISO date-time string") from None
    return v


_LEGAL_SUFFIXES = {"ltd", "limited", "inc", "llc", "gmbh", "bv", "srl", "plc", "sa", "ab", "as", "oy", "ag", "co", "corp"}


def company_key(name):
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _check_text(field, value, limit):
    if not isinstance(value, str):
        raise ValidationError(f"{field}: must be text")
    v = value.strip()
    if len(v) > limit:
        raise ValidationError(f"{field}: longer than {limit} characters")
    return v


def clean_lead_fields(payload, allow_extra=()):
    """Validate the fields present in payload. Absent keys are left out; None means 'no value'."""
    if not isinstance(payload, dict):
        raise ValidationError("each lead must be a JSON object")
    unknown = set(payload) - set(LEAD_FIELDS) - set(allow_extra)
    if unknown:
        raise ValidationError("unknown field(s): " + ", ".join(sorted(unknown)))
    out = {}
    for key, value in payload.items():
        if key in allow_extra:
            continue
        if key == "linkedin_url":
            out[key] = canonical_linkedin_url(value)
        elif value is None or value == "":
            out[key] = None
        elif key in TEXT_LIMITS:
            out[key] = _check_text(key, value, TEXT_LIMITS[key])
        elif key in URL_FIELDS:
            out[key] = check_http_url(key, value)
        elif key in DATE_FIELDS:
            out[key] = check_date(key, value)
        elif key in ENUM_FIELDS:
            if value not in ENUM_FIELDS[key]:
                raise ValidationError(f"{key}: one of {', '.join(ENUM_FIELDS[key])}")
            out[key] = value
        elif key in NUMBER_FIELDS:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= MAX_NUMBER or (isinstance(value, float) and not math.isfinite(value))):
                raise ValidationError(f"{key}: must be a number from 0 to {MAX_NUMBER:,}")
            out[key] = float(value)
    if out.get("budget_currency"):
        if not re.fullmatch(r"[A-Za-z]{3}", out["budget_currency"]):
            raise ValidationError("budget_currency: three-letter code such as EUR")
        out["budget_currency"] = out["budget_currency"].upper()
    if "company" in out:
        out["company_key"] = company_key(out["company"]) or None
    return out


def check_lead_record(rec, settings):
    """Rules on the merged record: claims need their source, budgets stay honest."""
    lo, hi = rec.get("budget_min"), rec.get("budget_max")
    if lo is not None and hi is not None and lo > hi:
        raise ValidationError("budget_min: greater than budget_max")
    if lo is not None or hi is not None:
        if not rec.get("budget_currency") or rec.get("budget_period") in (None, "unknown"):
            raise ValidationError("budget: a published number needs budget_currency and budget_period")
        if not rec.get("budget_source_url"):
            raise ValidationError("budget_source_url: required when a budget number is given (never inferred)")
    if rec.get("eligibility") not in (None, "unknown") and not rec.get("eligibility_source_url"):
        raise ValidationError("eligibility_source_url: required when eligibility is not 'unknown'")
    if rec.get("hiring_signal") and not rec.get("hiring_source_url"):
        raise ValidationError("hiring_source_url: required when hiring_signal is given")
    limit = (settings.get("invitation") or {}).get("max_note_chars", 300)
    if rec.get("invitation_draft") and len(rec["invitation_draft"]) > limit:
        raise ValidationError(f"invitation_draft: longer than {limit} characters")


# ---------------------------------------------------------------- leads

def _row(row):
    return dict(row) if row is not None else None


def _event(conn, lead_id, kind, actor, *, from_status=None, to_status=None, evidence=None,
           evidence_url=None, detail=None, application_id=None):
    conn.execute(
        "INSERT INTO events (lead_id, application_id, kind, from_status, to_status, evidence, evidence_url,"
        " detail, actor, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (lead_id, application_id, kind, from_status, to_status, evidence, evidence_url,
         json.dumps(detail, sort_keys=True) if detail is not None else None, actor, now_iso()),
    )


def upsert_leads(conn, items, settings, actor="import"):
    """Insert or update leads by canonical URL, all in one transaction.

    Absent or null fields keep what is stored. A lead in an invitation state
    (pending/sent/connected/rejected) keeps that state whatever the payload says.
    """
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValidationError("expected a JSON object or a list of objects")
    prepared = []
    for i, item in enumerate(items):
        try:
            fields = clean_lead_fields(item, allow_extra=UPSERT_EXTRA)
            if "linkedin_url" not in fields:
                raise ValidationError("linkedin_url: required")
            status = item.get("status")
            if status is not None and (not isinstance(status, str) or status not in RESEARCH_STATUSES):
                raise ValidationError(
                    f"status: an import may set {', '.join(sorted(RESEARCH_STATUSES))}; "
                    "use update-status with evidence for invitation states")
            ev_url = item.get("status_evidence_url")
            if ev_url:
                ev_url = check_http_url("status_evidence_url", ev_url)
            ev = item.get("status_evidence")
            if ev is not None:
                ev = _check_text("status_evidence", ev, 2000)
        except ValidationError as exc:
            raise ValidationError(f"item {i}: {exc}") from None
        prepared.append((fields, status, ev, ev_url))

    results = []
    stamp = now_iso()
    with _tx(conn):
        for i, (fields, status, ev, ev_url) in enumerate(prepared):
            url = fields["linkedin_url"]
            existing = _row(conn.execute("SELECT * FROM leads WHERE linkedin_url = ?", (url,)).fetchone())
            if existing and existing.get("sync_deleted"):
                raise ValidationError("record is deleted; explicitly restore it before importing")
            incoming = {k: v for k, v in fields.items() if v is not None and k != "linkedin_url"}
            merged = dict(existing or {}, **incoming)
            try:
                check_lead_record(merged, settings)
            except ValidationError as exc:
                raise ValidationError(f"item {i}: {exc}") from None
            if existing is None:
                if status not in (None, "research") and not ev:
                    raise ValidationError(f"item {i}: status_evidence: required to create a lead as '{status}'")
                cols = ["linkedin_url", "status", "created_at", "updated_at"] + list(incoming)
                vals = [url, status or "research", stamp, stamp] + list(incoming.values())
                cur = conn.execute(
                    f"INSERT INTO leads ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
                lead_id = cur.lastrowid
                _event(conn, lead_id, "created", actor, to_status=status or "research",
                       evidence=ev, evidence_url=ev_url, detail={"fields": sorted(incoming)})
                results.append({"id": lead_id, "url": url, "action": "created"})
                continue
            lead_id = existing["id"]
            changed = {k: [existing.get(k), v] for k, v in incoming.items() if existing.get(k) != v}
            result = {"id": lead_id, "url": url, "action": "updated" if changed else "unchanged"}
            if changed:
                sets = ", ".join(f"{k} = ?" for k in changed) + ", updated_at = ?"
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?",
                             [v[1] for v in changed.values()] + [stamp, lead_id])
                _event(conn, lead_id, "updated", actor, detail={"changed": changed})
                result["changed"] = sorted(changed)
            if status and status != existing["status"]:
                if existing["status"] in PROTECTED_STATUSES:
                    result["preserved_status"] = existing["status"]
                elif status not in TRANSITIONS[existing["status"]]:
                    raise ValidationError(
                        f"item {i}: status: '{existing['status']}' can move to "
                        f"{', '.join(sorted(TRANSITIONS[existing['status']]))}, not '{status}'")
                elif not ev:
                    raise ValidationError(f"item {i}: status_evidence: required to change status to '{status}'")
                else:
                    conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
                                 (status, stamp, lead_id))
                    _event(conn, lead_id, "status", actor, from_status=existing["status"], to_status=status,
                           evidence=ev, evidence_url=ev_url)
                    result["action"] = "updated"
                    result["status"] = [existing["status"], status]
            results.append(result)
    return results


def edit_lead(conn, lead_id, payload, settings, actor="ui"):
    """Edit fields of one lead (not its status). Null or empty clears a field."""
    fields = clean_lead_fields(payload)
    if "linkedin_url" in fields:
        raise ValidationError("linkedin_url: cannot be edited; it is the lead's key")
    with _tx(conn):
        existing = _row(conn.execute("SELECT * FROM leads WHERE sync_deleted = 0 AND id = ?", (lead_id,)).fetchone())
        if existing is None:
            raise NotFound(f"lead {lead_id} not found")
        for key in ENUM_FIELDS:
            if key in fields and fields[key] is None:
                fields[key] = "unknown"
        merged = dict(existing, **fields)
        check_lead_record(merged, settings)
        changed = {k: [existing.get(k), v] for k, v in fields.items() if existing.get(k) != v}
        if changed:
            sets = ", ".join(f"{k} = ?" for k in changed) + ", updated_at = ?"
            conn.execute(f"UPDATE leads SET {sets} WHERE id = ?",
                         [v[1] for v in changed.values()] + [now_iso(), lead_id])
            _event(conn, lead_id, "updated", actor, detail={"changed": changed})
    return {"id": lead_id, "changed": sorted(changed)}


def update_status(conn, lead_id, to_status, evidence, evidence_url=None, actor="cli"):
    """Move a lead along the workflow. Evidence is required; the event is permanent."""
    if to_status not in STATUSES:
        raise ValidationError(f"status: one of {', '.join(STATUSES)}")
    evidence = _check_text("evidence", evidence or "", 2000)
    if not evidence:
        raise ValidationError("evidence: say what you saw (and where) that justifies this status")
    if evidence_url:
        evidence_url = check_http_url("evidence_url", evidence_url)
    with _tx(conn):
        row = conn.execute("SELECT status FROM leads WHERE sync_deleted = 0 AND id = ?", (lead_id,)).fetchone()
        if row is None:
            raise NotFound(f"lead {lead_id} not found")
        current = row["status"]
        if to_status == current:
            raise ValidationError(f"status: lead is already '{current}'")
        if to_status not in TRANSITIONS[current]:
            allowed = ", ".join(sorted(TRANSITIONS[current])) or "nothing (final)"
            raise ValidationError(f"status: '{current}' can move to {allowed}, not '{to_status}'")
        conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (to_status, now_iso(), lead_id))
        _event(conn, lead_id, "status", actor, from_status=current, to_status=to_status,
               evidence=evidence, evidence_url=evidence_url)
    return {"id": lead_id, "status": [current, to_status]}


def find_lead(conn, ref):
    """Look a lead up by id or by any form of its LinkedIn URL."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isascii() and ref.isdigit()):
        if not 0 < int(ref) < MAX_ID:
            raise NotFound(f"lead {ref} not found")
        row = conn.execute("SELECT * FROM leads WHERE sync_deleted = 0 AND id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM leads WHERE sync_deleted = 0 AND linkedin_url = ?", (canonical_linkedin_url(ref),)).fetchone()
    if row is None:
        raise NotFound(f"lead {ref} not found")
    return dict(row)


def lead_events(conn, lead_id):
    rows = conn.execute("SELECT * FROM events WHERE lead_id = ? ORDER BY id", (lead_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def list_leads(conn, status=None, q=None, limit=500):
    sql, args = "SELECT * FROM leads WHERE sync_deleted = 0", []
    if status:
        if status not in STATUSES:
            raise ValidationError(f"status: one of {', '.join(STATUSES)}")
        sql += " AND status = ?"
        args.append(status)
    if q:
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (" AND (lower(coalesce(name,'')) LIKE ? ESCAPE '\\' OR lower(coalesce(company,'')) LIKE ? ESCAPE '\\'"
                " OR lower(coalesce(title,'')) LIKE ? ESCAPE '\\' OR lower(linkedin_url) LIKE ? ESCAPE '\\')")
        args += [like] * 4
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- queue

def _fold(text):
    """Lowercase without accents, so 'România' and 'Romania' compare equal."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def excluded_location(country, location, settings):
    """The excluded country a contact's stated location points to, or None.

    The country field matches a listed code exactly, or contains a country name as whole
    words; the free-text location is searched for country names, and for major cities only
    when no country is recorded (city names repeat across countries). Codes are never searched
    inside text. Location only: names and nationality are never looked at.
    """
    aliases = settings.get("excluded_contact_country_aliases") or {}
    words = lambda t: " ".join(re.findall(r"\w+", _fold(t)))
    folded_country = words(country)
    padded_country = f" {folded_country} "
    padded_location = f" {words(location)} "
    for name in settings.get("excluded_contact_countries", []):
        a = aliases.get(name, {})
        codes = {words(x) for x in a.get("codes", [])}
        names = {words(x) for x in [name] + a.get("names", [])} - {""}
        cities = {words(x) for x in a.get("cities", [])} - {""}
        if folded_country in codes or any(f" {n} " in padded_country for n in names):
            return name
        searched = names if folded_country else names | cities
        if any(f" {n} " in padded_location for n in searched):
            return name
    return None


def queue_blockers(lead, settings):
    """Reasons a lead may not be invited yet; an empty list means it is ready."""
    reasons = []
    if lead.get("sync_conflicted"):
        reasons.append("unresolved sync conflict: review the conflicting revisions first")
    if lead["status"] != "qualified":
        reasons.append(f"status is '{lead['status']}', not 'qualified'")
    if lead.get("hold_reason"):
        reasons.append("on hold: " + lead["hold_reason"])
    country = (lead.get("contact_country") or "").strip()
    hit = excluded_location(country, lead.get("contact_location"), settings)
    if hit:
        reasons.append(f"contact located in {hit} (excluded by policy)")
    elif not country:
        reasons.append("contact country unknown: check the profile location")
    elig = lead.get("eligibility") or "unknown"
    if elig in settings.get("rejected_eligibility", []):
        reasons.append(f"role restricted ({elig})")
    elif elig not in settings.get("required_eligibility", []):
        reasons.append("hiring eligibility unverified (needs worldwide or Europe including Romania)")
    elif not lead.get("eligibility_source_url"):
        reasons.append("eligibility has no source link")
    if (lead.get("company_type") or "unknown") in settings.get("skip_company_types", []):
        reasons.append(f"company type '{lead['company_type']}' is skipped by policy")
    draft = (lead.get("invitation_draft") or "").strip()
    limit = (settings.get("invitation") or {}).get("max_note_chars", 300)
    if not draft:
        reasons.append("no invitation draft")
    elif len(draft) > limit:
        reasons.append(f"invitation draft longer than {limit} characters")
    return reasons


def budget_view(lead, settings):
    """Describe the budget without inventing anything: no conversions, no country guesses."""
    lo, hi = lead.get("budget_min"), lead.get("budget_max")
    if lo is None and hi is None:
        return {"published": False, "text": "unknown", "meets_goal": None}
    cur, per = lead.get("budget_currency"), lead.get("budget_period")
    fmt = lambda n: f"{n:,.0f}"
    rng = f"{fmt(lo)}-{fmt(hi)}" if lo is not None and hi is not None else fmt(lo if lo is not None else hi)
    goal = settings.get("goal") or {}
    meets = None
    if cur == goal.get("currency") and per == goal.get("period"):
        top = hi if hi is not None else lo
        meets = top >= goal.get("amount", 0)
    return {"published": True, "text": f"{rng} {cur} per {per}", "meets_goal": meets}


def queue(conn, settings, explain=False):
    leads = conn.execute("SELECT * FROM leads WHERE sync_deleted = 0 AND status = 'qualified'").fetchall()
    preferred = {m.lower() for m in settings.get("preferred_company_markets", [])}
    ready, blocked = [], []
    for row in leads:
        lead = dict(row)
        reasons = queue_blockers(lead, settings)
        if reasons:
            if explain:
                blocked.append({"id": lead["id"], "name": lead["name"], "company": lead["company"], "why": reasons})
            continue
        ready.append(lead)
    ready.sort(key=lambda l: (
        (l.get("company_market") or "").lower() not in preferred,
        l.get("budget_min") is None and l.get("budget_max") is None,
        l["updated_at"],
    ))
    out = {"ready": [{
        "id": l["id"], "url": l["linkedin_url"], "name": l["name"], "company": l["company"],
        "title": l["title"], "budget": budget_view(l, settings)["text"], "draft": l["invitation_draft"],
    } for l in ready]}
    if explain:
        out["blocked"] = blocked
    return out


# ---------------------------------------------------------------- applications

def clean_application(payload):
    if not isinstance(payload, dict):
        raise ValidationError("each application must be a JSON object")
    unknown = set(payload) - set(APP_FIELDS)
    if unknown:
        raise ValidationError("unknown field(s): " + ", ".join(sorted(unknown)))
    out = {}
    for key, value in payload.items():
        if value is None or value == "":
            out[key] = None
        elif key in APP_TEXT_LIMITS:
            out[key] = _check_text(key, value, APP_TEXT_LIMITS[key])
        elif key in ("applied_date", "rejection_date"):
            out[key] = check_date(key, value)
        elif key == "evidence_url":
            out[key] = check_http_url(key, value)
        elif key in APP_ENUMS:
            if value not in APP_ENUMS[key]:
                raise ValidationError(f"{key}: one of {', '.join(APP_ENUMS[key])}")
            out[key] = value
    return out


def check_application(rec):
    if not rec.get("company"):
        raise ValidationError("company: required")
    basis = rec.get("decision_basis") or "unknown"
    source = rec.get("rejection_source") or "unknown"
    if rec.get("outcome") == "rejected" and not (rec.get("evidence") or rec.get("evidence_url")):
        raise ValidationError("evidence: a recorded rejection needs evidence (what the message or portal said)")
    if basis == "automated_email_only" and source != "automated_email":
        raise ValidationError("decision_basis: automated_email_only needs rejection_source automated_email")
    if basis == "ats_auto_reject_proven" and source not in ("ats_status", "automated_email"):
        raise ValidationError("decision_basis: ats_auto_reject_proven needs an ATS status or automated email source")
    if basis == "ats_auto_reject_proven" and not rec.get("evidence"):
        raise ValidationError("evidence: say what proves the ATS decided alone (e.g. knockout question, portal status)")
    if basis == "human_screening_confirmed" and source not in ("recruiter_message", "hiring_manager_message"):
        raise ValidationError("decision_basis: human_screening_confirmed needs a recruiter or hiring manager message")


def upsert_applications(conn, items, actor="import"):
    """Record past applications. Keyed by company, role and applied date; one transaction."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValidationError("expected a JSON object or a list of objects")
    prepared = []
    for i, item in enumerate(items):
        try:
            fields = clean_application(item)
            if not fields.get("company"):
                raise ValidationError("company: required")
        except ValidationError as exc:
            raise ValidationError(f"item {i}: {exc}") from None
        fields["company_key"] = company_key(fields["company"])
        fields.setdefault("role_title", None)
        fields.setdefault("applied_date", None)
        prepared.append(fields)
    results = []
    stamp = now_iso()
    with _tx(conn):
        for i, fields in enumerate(prepared):
            key = (fields["company_key"], fields["role_title"] or "", fields["applied_date"] or "")
            existing = _row(conn.execute(
                "SELECT * FROM applications WHERE company_key = ? AND role_title = ? AND applied_date = ?",
                key).fetchone())
            if existing and existing.get("sync_deleted"):
                raise ValidationError("record is deleted; explicitly restore it before importing")
            incoming = {k: v for k, v in fields.items() if v is not None}
            incoming["role_title"], incoming["applied_date"] = key[1], key[2]
            merged = dict(existing or {}, **incoming)
            try:
                check_application(merged)
            except ValidationError as exc:
                raise ValidationError(f"item {i}: {exc}") from None
            if existing is None:
                cols = list(incoming) + ["created_at", "updated_at"]
                cur = conn.execute(
                    f"INSERT INTO applications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    list(incoming.values()) + [stamp, stamp])
                _event(conn, None, "application_created", actor, application_id=cur.lastrowid,
                       evidence=merged.get("evidence"), evidence_url=merged.get("evidence_url"),
                       detail={"company": merged["company"], "outcome": merged.get("outcome"),
                               "decision_basis": merged.get("decision_basis")})
                results.append({"id": cur.lastrowid, "company": merged["company"], "action": "created"})
                continue
            changed = {k: [existing.get(k), v] for k, v in incoming.items() if existing.get(k) != v}
            if changed:
                sets = ", ".join(f"{k} = ?" for k in changed) + ", updated_at = ?"
                conn.execute(f"UPDATE applications SET {sets} WHERE id = ?",
                             [v[1] for v in changed.values()] + [stamp, existing["id"]])
                _event(conn, None, "application_updated", actor, application_id=existing["id"],
                       detail={"changed": changed})
            results.append({"id": existing["id"], "company": existing["company"],
                            "action": "updated" if changed else "unchanged"})
    return results


def set_revisit_status(conn, app_id, revisit_status, actor="ui"):
    if revisit_status not in REVISIT_STATUSES:
        raise ValidationError(f"revisit_status: one of {', '.join(REVISIT_STATUSES)}")
    with _tx(conn):
        row = conn.execute("SELECT revisit_status FROM applications WHERE sync_deleted = 0 AND id = ?", (app_id,)).fetchone()
        if row is None:
            raise NotFound(f"application {app_id} not found")
        conn.execute("UPDATE applications SET revisit_status = ?, updated_at = ? WHERE id = ?",
                     (revisit_status, now_iso(), app_id))
        _event(conn, None, "application_revisit", actor, application_id=app_id,
               detail={"revisit_status": [row["revisit_status"], revisit_status]})
    return {"id": app_id, "revisit_status": revisit_status}


def list_applications(conn, revisit_only=False, settings=None):
    rows = [dict(r) for r in conn.execute(
        "SELECT a.*, (SELECT count(*) FROM leads l WHERE l.company_key = a.company_key AND l.sync_deleted = 0) AS lead_count"
        " FROM applications a WHERE a.sync_deleted = 0 ORDER BY a.updated_at DESC, a.id DESC").fetchall()]
    if revisit_only:
        bases = set((settings or {}).get("application_revisit", {}).get(
            "worth_revisiting_bases", ["automated_email_only", "ats_auto_reject_proven", "unknown"]))
        rows = [r for r in rows if r["outcome"] == "rejected" and r["decision_basis"] in bases
                and r["revisit_status"] not in ("done", "skip")]
    return rows


def company_applications(conn, key):
    if not key:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM applications WHERE sync_deleted = 0 AND company_key = ? ORDER BY applied_date DESC", (key,)).fetchall()]


# ---------------------------------------------------------------- runs, stats, export

def record_run(conn, payload):
    if not isinstance(payload, dict):
        raise ValidationError("run must be a JSON object")
    allowed = {"kind", "started_at", "finished_at", "summary", "counts", "notes"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValidationError("unknown field(s): " + ", ".join(sorted(unknown)))
    kind = _check_text("kind", payload.get("kind") or "", 100)
    summary = _check_text("summary", payload.get("summary") or "", 2000)
    if not kind or not summary:
        raise ValidationError("kind and summary: required")
    started = check_date("started_at", payload["started_at"]) if payload.get("started_at") else None
    finished = check_date("finished_at", payload["finished_at"]) if payload.get("finished_at") else None
    counts = payload.get("counts")
    if counts is not None:
        if not isinstance(counts, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in counts.items()):
            raise ValidationError("counts: an object of name -> whole number")
    notes = _check_text("notes", payload["notes"], 5000) if payload.get("notes") else None
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO runs (kind, started_at, finished_at, summary, counts, notes, recorded_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, started, finished, summary, json.dumps(counts) if counts is not None else None, notes, now_iso()))
    return {"id": cur.lastrowid}


def list_runs(conn, limit=50):
    out = []
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall():
        d = dict(r)
        d["counts"] = json.loads(d["counts"]) if d["counts"] else None
        out.append(d)
    return out


def stats(conn, settings):
    by_status = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, count(*) n FROM leads WHERE sync_deleted = 0 GROUP BY status"):
        by_status[r["status"]] = r["n"]
    return {
        "leads": sum(by_status.values()),
        "by_status": by_status,
        "queue_ready": len(queue(conn, settings)["ready"]),
        "applications": conn.execute("SELECT count(*) FROM applications WHERE sync_deleted = 0").fetchone()[0],
        "revisit_open": len(list_applications(conn, revisit_only=True, settings=settings)),
        "runs": conn.execute("SELECT count(*) FROM runs").fetchone()[0],
    }


EXPORT_COLUMNS = ("id", "linkedin_url", "status") + tuple(k for k in LEAD_FIELDS if k != "linkedin_url") + (
    "created_at", "updated_at")


def export_leads(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM leads WHERE sync_deleted = 0 ORDER BY id").fetchall()]
