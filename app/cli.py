"""Command line for agents and scripts. Every command prints compact JSON on stdout.

Errors go to stderr as {"error": ...} with exit code 2 (bad input) or 3 (not found).
"""

import argparse
import csv
import io
import json
import sqlite3
import sys

from . import core


def _out(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n")


def _read_json(path):
    try:
        if path in (None, "-"):
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
    except UnicodeDecodeError:
        raise core.ValidationError("input is not UTF-8; save the file as UTF-8") from None
    try:
        return json.loads(text, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise core.ValidationError(f"input is not valid JSON: {exc}") from None


def _no_constant(name):
    raise core.ValidationError(f"input contains {name}; use a finite number or null")


def _utf8_streams():
    # Windows pipes default to the ANSI code page; names like "Ștefan" need UTF-8 both ways.
    for stream, enc in ((sys.stdin, "utf-8-sig"), (sys.stdout, "utf-8"), (sys.stderr, "utf-8")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding=enc)


COMPACT = ("id", "linkedin_url", "name", "company", "title", "status", "contact_country", "eligibility", "updated_at")


def build_parser():
    p = argparse.ArgumentParser(prog="python -m app", description="Local LinkedIn lead workbench (no network access).")
    p.add_argument("--db", help="SQLite file (default app/data/leads.sqlite3 or $LNAPP_DB)")
    p.add_argument("--settings", help="policy JSON (default app/settings.json, else app/settings.example.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database if missing")

    s = sub.add_parser("upsert", help="insert/update leads from a JSON object or list")
    s.add_argument("file", nargs="?", default="-", help="JSON file, or - for stdin")
    s.add_argument("--actor", default="import")

    s = sub.add_parser("list", help="list leads (compact)")
    s.add_argument("--status", choices=core.STATUSES)
    s.add_argument("--q", help="search name, company, title, URL")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--full", action="store_true", help="all fields")

    s = sub.add_parser("get", help="one lead with its events, by id or LinkedIn URL")
    s.add_argument("ref")

    s = sub.add_parser("queue", help="qualified leads ready for a person to invite")
    s.add_argument("--explain", action="store_true", help="also list qualified leads that are blocked, and why")

    s = sub.add_parser("update-status", help="move a lead along the workflow, with evidence")
    s.add_argument("ref", help="lead id or LinkedIn URL")
    s.add_argument("status", choices=core.STATUSES)
    s.add_argument("--evidence", required=True, help="what you saw that justifies it")
    s.add_argument("--evidence-url")
    s.add_argument("--actor", default="cli")

    s = sub.add_parser("upsert-applications", help="record past job applications (JSON object or list)")
    s.add_argument("file", nargs="?", default="-")
    s.add_argument("--actor", default="import")

    s = sub.add_parser("applications", help="list recorded applications")
    s.add_argument("--revisit", action="store_true", help="only rejections worth revisiting through people")

    s = sub.add_parser("export", help="export leads")
    s.add_argument("--format", choices=("json", "csv"), default="json")
    s.add_argument("--out", help="file to write (default stdout)")

    sub.add_parser("stats", help="counts by status, queue size, revisit list")

    s = sub.add_parser("record-run", help="log a research/invitation session (JSON object)")
    s.add_argument("file", nargs="?", default="-")

    s = sub.add_parser("runs", help="recent runs")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("policy", help="print the current policy")

    s = sub.add_parser("serve", help="run the local UI on 127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    return p


def run(argv=None):
    _utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        settings = core.load_settings(args.settings)
        if args.cmd == "policy":
            return _out(settings)
        if args.cmd == "serve":
            from . import server
            return server.serve(args.db, settings, args.port)
        conn = core.connect(args.db)
        try:
            _dispatch(args, conn, settings)
        finally:
            conn.close()
    except core.ValidationError as exc:
        sys.stderr.write(json.dumps({"error": str(exc)}) + "\n")
        return 2
    except core.NotFound as exc:
        sys.stderr.write(json.dumps({"error": str(exc)}) + "\n")
        return 3
    except FileExistsError as exc:
        sys.stderr.write(json.dumps({"error": f"{exc.filename} already exists; choose a new file"}) + "\n")
        return 2
    except (OSError, sqlite3.Error) as exc:
        sys.stderr.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        return 2
    return 0


def _dispatch(args, conn, settings):
    cmd = args.cmd
    if cmd == "init":
        _out({"ok": True, "leads": conn.execute("SELECT count(*) FROM leads").fetchone()[0]})
    elif cmd == "upsert":
        res = core.upsert_leads(conn, _read_json(args.file), settings, actor=args.actor)
        _out({"results": res})
    elif cmd == "list":
        rows = core.list_leads(conn, status=args.status, q=args.q, limit=args.limit)
        _out(rows if args.full else [{k: r[k] for k in COMPACT} for r in rows])
    elif cmd == "get":
        lead = core.find_lead(conn, args.ref)
        lead["queue_blockers"] = core.queue_blockers(lead, settings)
        lead["budget"] = core.budget_view(lead, settings)
        lead["events"] = core.lead_events(conn, lead["id"])
        lead["applications"] = core.company_applications(conn, lead["company_key"])
        _out(lead)
    elif cmd == "queue":
        _out(core.queue(conn, settings, explain=args.explain))
    elif cmd == "update-status":
        lead = core.find_lead(conn, args.ref)
        _out(core.update_status(conn, lead["id"], args.status, args.evidence, args.evidence_url, actor=args.actor))
    elif cmd == "upsert-applications":
        _out({"results": core.upsert_applications(conn, _read_json(args.file), actor=args.actor)})
    elif cmd == "applications":
        _out(core.list_applications(conn, revisit_only=args.revisit, settings=settings))
    elif cmd == "export":
        rows = core.export_leads(conn)
        if args.format == "json":
            text = json.dumps(rows, ensure_ascii=False, indent=1) + "\n"
        else:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=core.EXPORT_COLUMNS, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for r in rows:
                # Spreadsheet apps run cells starting with = + - @ as formulas; prefix them.
                w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
            text = buf.getvalue()
        if args.out:
            with open(args.out, "x", encoding="utf-8", newline="") as fh:
                fh.write(text)
            _out({"written": args.out, "leads": len(rows)})
        else:
            sys.stdout.write(text)
    elif cmd == "stats":
        _out(core.stats(conn, settings))
    elif cmd == "record-run":
        _out(core.record_run(conn, _read_json(args.file)))
    elif cmd == "runs":
        _out(core.list_runs(conn, args.limit))


def main():
    sys.exit(run())
