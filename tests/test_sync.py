import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import core, sync

URL = "https://www.linkedin.com/in/workbench-fictional-person"


class MemoryRemote:
    identity = "fictional-test-account"

    def __init__(self):
        self.operations = {}
        self.fail_put = False
        self.on_list = None

    def put(self, ident, revision):
        self.operations[ident] = copy.deepcopy(revision)
        if self.fail_put:
            raise OSError("connection lost after remote accepted upload")

    def list_operations(self):
        if self.on_list:
            callback, self.on_list = self.on_list, None
            callback()
        return copy.deepcopy(list(self.operations.values()))


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a_path = Path(self.tmp.name) / "a.sqlite3"
        self.a = core.connect(self.a_path)
        self.b = core.connect(Path(self.tmp.name) / "b.sqlite3")
        self.remote = MemoryRemote()
        self.policy = core.load_settings(core.APP_DIR / "settings.example.json")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def lead(self, conn, **fields):
        return core.upsert_leads(conn, {"linkedin_url": URL, "company": "Fictional Systems", **fields}, self.policy)

    def exchange(self):
        sync.synchronize(self.a, self.remote)
        sync.synchronize(self.b, self.remote)
        return sync.synchronize(self.a, self.remote)

    def test_powered_off_laptop_and_restart(self):
        self.lead(self.a, name="Fictional Person")
        sync.synchronize(self.a, self.remote)
        self.a.close()  # Laptop A can now be switched off.
        sync.synchronize(self.b, self.remote)
        self.assertEqual(core.find_lead(self.b, URL)["name"], "Fictional Person")
        self.lead(self.b, title="Updated on B")
        sync.synchronize(self.b, self.remote)
        self.a = core.connect(self.a_path)
        sync.synchronize(self.a, self.remote)
        self.assertEqual(core.find_lead(self.a, URL)["title"], "Updated on B")

    def test_offline_edits_and_lost_upload_response_retry(self):
        self.lead(self.a, notes="Saved offline")
        self.remote.fail_put = True
        with self.assertRaises(OSError):
            sync.synchronize(self.a, self.remote)
        self.assertEqual(core.find_lead(self.a, URL)["notes"], "Saved offline")
        self.remote.fail_put = False
        self.exchange()
        self.assertEqual(core.find_lead(self.b, URL)["notes"], "Saved offline")
        before = dict(self.remote.operations)
        self.exchange()
        self.assertEqual(self.remote.operations, before)
        self.assertEqual(core.stats(self.b, self.policy)["leads"], 1)

    def test_concurrent_changes_require_resolution_then_converge(self):
        self.lead(self.a)
        self.exchange()
        self.lead(self.a, notes="A changed it")
        self.lead(self.b, notes="B changed it")
        self.exchange()
        ca, cb = sync.conflicts(self.a), sync.conflicts(self.b)
        self.assertEqual(ca, cb)
        self.assertEqual(len(ca), 1)
        self.assertTrue(any("sync conflict" in b for b in core.queue_blockers(core.find_lead(self.a, URL), self.policy)))
        chosen = next(v["revision"] for v in ca[0]["versions"] if v["value"]["notes"] == "B changed it")
        sync.resolve(self.a, ca[0]["record"], chosen)
        self.exchange()
        self.assertEqual(sync.conflicts(self.b), [])
        self.assertEqual(core.find_lead(self.b, URL)["sync_conflicted"], 0)
        self.assertEqual(core.find_lead(self.a, URL)["notes"], "B changed it")
        self.assertEqual(core.find_lead(self.b, URL)["notes"], "B changed it")

    def test_new_local_edit_on_conflict_not_silently_overwritten(self):
        self.lead(self.a)
        self.exchange()
        self.lead(self.a, notes="A")
        self.lead(self.b, notes="B")
        self.exchange()
        self.lead(self.a, notes="new unsynced edit")
        with self.assertRaisesRegex(core.ValidationError, "local edit to conflicted"):
            sync.synchronize(self.a, self.remote)
        self.assertEqual(core.find_lead(self.a, URL)["notes"], "new unsynced edit")

    def test_resolving_one_conflict_preserves_other_unsynced_records(self):
        other = "https://www.linkedin.com/in/fictional-other-unsynced"
        self.lead(self.a)
        self.lead(self.a, linkedin_url=other, notes="original")
        self.exchange()
        self.lead(self.a, notes="A")
        self.lead(self.b, notes="B")
        self.exchange()
        self.lead(self.a, linkedin_url=other, notes="unsynced must survive")
        conflict = sync.conflicts(self.a)[0]
        sync.resolve(self.a, conflict["record"], conflict["versions"][0]["revision"])
        self.assertEqual(core.find_lead(self.a, other)["notes"], "unsynced must survive")
        self.exchange()
        self.assertEqual(core.find_lead(self.b, other)["notes"], "unsynced must survive")

    def test_delete_syncs_and_stale_device_cannot_resurrect(self):
        self.lead(self.a)
        self.exchange()
        sync.set_deleted(self.a, "lead", 1)
        self.exchange()
        for conn in (self.a, self.b):
            self.assertEqual(core.list_leads(conn), [])
            self.assertEqual(core.export_leads(conn), [])
            with self.assertRaises(core.NotFound):
                core.find_lead(conn, URL)
            with self.assertRaisesRegex(core.ValidationError, "deleted"):
                self.lead(conn, notes="accidental import")
        sync.set_deleted(self.b, "lead", 1, False)
        self.exchange()
        self.assertEqual(core.find_lead(self.a, URL)["sync_deleted"], 0)

    def test_delete_vs_offline_edit_is_conflict(self):
        self.lead(self.a)
        self.exchange()
        sync.set_deleted(self.a, "lead", 1)
        self.lead(self.b, notes="offline change")
        self.exchange()
        versions = sync.conflicts(self.b)[0]["versions"]
        self.assertEqual({v["value"]["sync_deleted"] for v in versions}, {0, 1})

    def test_edit_during_network_io_is_captured_before_download(self):
        self.lead(self.a)
        self.exchange()
        self.lead(self.b, notes="remote change")
        sync.synchronize(self.b, self.remote)
        self.remote.on_list = lambda: self.lead(self.a, notes="local during network")
        result = sync.synchronize(self.a, self.remote)
        self.assertTrue(result["conflicts"])
        self.assertEqual(core.find_lead(self.a, URL)["notes"], "local during network")
        self.assertGreater(result["pending_upload"], 0)

    def test_application_history_runs_and_device_local_ids(self):
        self.lead(self.a)
        self.lead(self.b, linkedin_url="https://www.linkedin.com/in/unrelated-fictional-person")
        app = {"company": "Fictional Systems", "role_title": "Engineer", "applied_date": "2026-01-01", "outcome": "applied"}
        core.upsert_applications(self.a, app)
        core.record_run(self.a, {"kind": "research", "summary": "Fictional test run"})
        self.exchange()
        self.assertEqual(len(core.list_applications(self.b)), 1)
        lead = core.find_lead(self.b, URL)
        self.assertNotEqual(lead["id"], core.find_lead(self.a, URL)["id"])
        self.assertEqual(len(core.lead_events(self.b, lead["id"])), 1)
        self.assertEqual(len(core.list_runs(self.b)), 1)
        self.exchange()
        self.assertEqual(len(core.list_runs(self.b)), 1)
        sync.set_deleted(self.a, "application", 1)
        self.exchange()
        self.assertEqual(core.list_applications(self.b), [])

    def test_duplicate_revisions_are_idempotent(self):
        self.lead(self.a)
        sync.synchronize(self.a, self.remote)
        revisions = self.remote.list_operations()
        sync.install(self.b)
        with core._tx(self.b):
            sync.ingest(self.b, revisions + revisions)
        self.assertEqual(len(core.list_leads(self.b)), 1)

    def test_corrupt_remote_rolls_back_and_preserves_local_work(self):
        self.lead(self.a)
        sync.synchronize(self.a, self.remote)
        ops = self.remote.list_operations()
        ops[0]["value"]["company"] = "tampered"
        sync.install(self.b)
        with self.assertRaises(core.ValidationError), core._tx(self.b):
            sync.ingest(self.b, ops)
        self.assertEqual(core.list_leads(self.b), [])
        self.assertEqual(sync.all_ops(self.b), {})

    def test_missing_parent_retries_without_losing_revision(self):
        self.lead(self.a)
        sync.synchronize(self.a, self.remote)
        self.lead(self.a, notes="second version")
        sync.synchronize(self.a, self.remote)
        revision = next(op for op in self.remote.operations.values() if op["record"].startswith("lead:") and op["parents"])
        sync.install(self.b)
        with self.assertRaisesRegex(core.ValidationError, "incomplete"), core._tx(self.b):
            sync.ingest(self.b, [revision])
        sync.synchronize(self.b, self.remote)
        self.assertEqual(core.find_lead(self.b, URL)["notes"], "second version")

    def test_local_policy_does_not_destroy_long_remote_draft(self):
        self.lead(self.a, invitation_draft="x" * 299)
        self.exchange()
        tighter = dict(self.policy, invitation={"max_note_chars": 200})
        self.assertTrue(any("longer" in b for b in core.queue_blockers(core.find_lead(self.b, URL), tighter)))

    def test_concurrent_same_record_creation_deduplicates_natural_key(self):
        self.lead(self.a, notes="A")
        self.lead(self.b, notes="B")
        self.exchange()
        self.assertEqual(len(core.list_leads(self.a)), 1)
        self.assertEqual(len(core.list_leads(self.b)), 1)
        self.assertEqual(len(sync.conflicts(self.a)), 1)

    def test_fresh_third_laptop_can_download_unresolved_conflict_and_history(self):
        self.lead(self.a, notes="A")
        self.lead(self.b, notes="B")
        self.exchange()
        third = core.connect(Path(self.tmp.name) / "third.sqlite3")
        try:
            result = sync.synchronize(third, self.remote)
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertEqual(len(core.list_leads(third)), 1)
            self.assertEqual(sync.synchronize(third, self.remote)["uploaded"], 0)
        finally:
            third.close()

    def test_no_credentials_or_local_policy_in_synced_revisions(self):
        self.lead(self.a)
        self.exchange()
        payload = json.dumps(self.remote.list_operations())
        for word in ("refresh_token", "client_secret", "excluded_contact_countries", "invitation_limit"):
            self.assertNotIn(word, payload)


if __name__ == "__main__":
    unittest.main()
