import os
import sqlite3
import tempfile
import unittest

from app import core

# Explicit synthetic policy: tests do not depend on a user's private settings.
SETTINGS = core.load_settings(core.APP_DIR / "settings.example.json")
SETTINGS.update({
    "excluded_contact_countries": ["Exampleland"],
    "excluded_contact_country_aliases": {"Exampleland": {
        "codes": ["EX", "EXL"], "names": ["Exámpleland"], "cities": ["Sample City"]}},
    "required_eligibility": ["worldwide", "europe_including_romania"],
    "rejected_eligibility": ["us_only", "uk_only", "other_restricted"],
    "skip_company_types": ["low_cost_staff_augmentation"],
    "goal": {"amount": 7000, "currency": "EUR", "period": "month"},
})
P = "https://www.linkedin.com/in/jane-doe"


def ready_lead(**over):
    lead = {
        "linkedin_url": P, "name": "Jane Doe", "company": "Acme Ltd", "title": "Engineering Manager",
        "contact_location": "London, England, United Kingdom", "contact_country": "United Kingdom",
        "company_market": "UK", "eligibility": "worldwide",
        "eligibility_source_url": "https://acme.example/careers/remote",
        "invitation_draft": "Hi Jane, I saw Acme hires across Europe.", "status": "qualified",
        "status_evidence": "Profile and careers page checked 2026-11-11",
    }
    lead.update(over)
    return lead


class DBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.connect(os.path.join(self.tmp.name, "t.sqlite3"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class CanonicalUrl(unittest.TestCase):
    def test_variants_fold_to_one_key(self):
        for raw in [
            "https://www.linkedin.com/in/Jane-Doe/",
            "http://uk.linkedin.com/in/jane-doe?trk=abc&utm_source=x",
            "linkedin.com/in/jane-doe#about",
            "https://linkedin.com/in/jane-doe/details/experience/",
            "https://m.linkedin.com/in/jane%2Ddoe",
            "www.linkedin.com/in/jane-doe?r=https://x.example",
        ]:
            self.assertEqual(core.canonical_linkedin_url(raw), P, raw)

    def test_rejects_non_profiles(self):
        for raw in ["https://evil.com/in/jane", "https://linkedin.com.evil.com/in/x",
                    "https://www.linkedin.com/company/acme", "javascript:alert(1)",
                    "https://user:pw@www.linkedin.com/in/x", "https://www.linkedin.com/in/", "", None]:
            with self.assertRaises(core.ValidationError, msg=raw):
                core.canonical_linkedin_url(raw)


class Validation(DBTest):
    def test_unknown_field_rejected(self):
        with self.assertRaisesRegex(core.ValidationError, "unknown field"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "salary_guess": 9000}, SETTINGS)

    def test_source_links_must_be_http(self):
        with self.assertRaisesRegex(core.ValidationError, "hiring_source_url"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "hiring_signal": "x",
                                          "hiring_source_url": "javascript:alert(1)"}, SETTINGS)

    def test_budget_number_needs_source_currency_period(self):
        with self.assertRaisesRegex(core.ValidationError, "budget"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "budget_min": 5000}, SETTINGS)
        with self.assertRaisesRegex(core.ValidationError, "budget_source_url"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "budget_min": 5000, "budget_currency": "eur",
                                          "budget_period": "month"}, SETTINGS)

    def test_numbers_must_be_finite_and_bounded(self):
        base = {"linkedin_url": P, "budget_currency": "EUR", "budget_period": "month",
                "budget_source_url": "https://acme.example/job"}
        for bad in (float("inf"), float("nan"), 10**400, -1, True, "5000"):
            with self.assertRaises(core.ValidationError, msg=repr(bad)):
                core.upsert_leads(self.conn, dict(base, budget_min=bad), SETTINGS)

    def test_strict_dates(self):
        for bad in ("20261111", "2026-13-01", "11/11/2026", "2026-11-11T25:00"):
            with self.assertRaises(core.ValidationError, msg=bad):
                core.upsert_leads(self.conn, {"linkedin_url": P, "last_checked": bad}, SETTINGS)
        core.upsert_leads(self.conn, {"linkedin_url": P, "last_checked": "2026-11-11T10:00:00Z"}, SETTINGS)

    def test_huge_id_is_not_found(self):
        with self.assertRaises(core.NotFound):
            core.find_lead(self.conn, "9" * 30)
        with self.assertRaises(core.ValidationError):  # Unicode digits are not ids, nor a profile URL
            core.find_lead(self.conn, "²")

    def test_eligibility_claim_needs_source(self):
        with self.assertRaisesRegex(core.ValidationError, "eligibility_source_url"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "eligibility": "worldwide"}, SETTINGS)

    def test_draft_limit(self):
        with self.assertRaisesRegex(core.ValidationError, "invitation_draft"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "invitation_draft": "x" * 301}, SETTINGS)

    def test_batch_is_atomic(self):
        with self.assertRaises(core.ValidationError):
            core.upsert_leads(self.conn, [{"linkedin_url": P, "name": "Jane"},
                                          {"linkedin_url": "https://evil.com/in/x"}], SETTINGS)
        self.assertEqual(core.list_leads(self.conn), [])
        with self.assertRaises(core.ValidationError):  # second item fails a merged-record rule
            core.upsert_leads(self.conn, [{"linkedin_url": P, "name": "Jane"},
                                          {"linkedin_url": P, "eligibility": "worldwide"}], SETTINGS)
        self.assertEqual(core.list_leads(self.conn), [])


class Upsert(DBTest):
    def test_dedupe_and_merge(self):
        r1 = core.upsert_leads(self.conn, {"linkedin_url": "uk.linkedin.com/in/JANE-DOE/?trk=x", "name": "Jane"}, SETTINGS)
        r2 = core.upsert_leads(self.conn, {"linkedin_url": P + "/", "company": "Acme"}, SETTINGS)
        self.assertEqual(r1[0]["action"], "created")
        self.assertEqual(r2[0]["action"], "updated")
        rows = core.list_leads(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["company"]), ("Jane", "Acme"))
        r3 = core.upsert_leads(self.conn, {"linkedin_url": P, "name": None, "company": "Acme"}, SETTINGS)
        self.assertEqual(r3[0]["action"], "unchanged")
        self.assertEqual(core.find_lead(self.conn, P)["name"], "Jane")

    def test_import_cannot_set_invitation_state(self):
        with self.assertRaisesRegex(core.ValidationError, "update-status"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "status": "sent"}, SETTINGS)

    def test_preserves_invitation_state(self):
        core.upsert_leads(self.conn, ready_lead(), SETTINGS)
        lead = core.find_lead(self.conn, P)
        core.update_status(self.conn, lead["id"], "sent", "Sent by hand on LinkedIn 2026-11-11")
        res = core.upsert_leads(self.conn, ready_lead(status="research", title="Director"), SETTINGS)
        self.assertEqual(res[0]["preserved_status"], "sent")
        lead = core.find_lead(self.conn, P)
        self.assertEqual(lead["status"], "sent")
        self.assertEqual(lead["title"], "Director")  # research facts still update; the state does not

    def test_import_status_needs_evidence_and_legal_move(self):
        with self.assertRaisesRegex(core.ValidationError, "status_evidence"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "status": "qualified"}, SETTINGS)
        core.upsert_leads(self.conn, {"linkedin_url": P, "status": "excluded", "status_evidence": "UK-only"}, SETTINGS)
        with self.assertRaisesRegex(core.ValidationError, "can move to"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "status": "qualified", "status_evidence": "x"}, SETTINGS)
        with self.assertRaisesRegex(core.ValidationError, "status_evidence"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "status": "research"}, SETTINGS)
        with self.assertRaisesRegex(core.ValidationError, "status"):
            core.upsert_leads(self.conn, {"linkedin_url": P, "status": ["qualified"]}, SETTINGS)
        self.assertEqual(core.find_lead(self.conn, P)["status"], "excluded")

    def test_research_status_change_logged(self):
        core.upsert_leads(self.conn, {"linkedin_url": P}, SETTINGS)
        core.upsert_leads(self.conn, {"linkedin_url": P, "status": "excluded", "exclusion_reason": "US-only role",
                                      "status_evidence": "careers page says US only"}, SETTINGS)
        ev = [e for e in core.lead_events(self.conn, 1) if e["kind"] == "status"]
        self.assertEqual((ev[0]["from_status"], ev[0]["to_status"]), ("research", "excluded"))
        self.assertEqual(ev[0]["evidence"], "careers page says US only")


class Status(DBTest):
    def setUp(self):
        super().setUp()
        core.upsert_leads(self.conn, {"linkedin_url": P}, SETTINGS)

    def test_requires_evidence(self):
        with self.assertRaisesRegex(core.ValidationError, "evidence"):
            core.update_status(self.conn, 1, "qualified", "  ")

    def test_illegal_transition(self):
        with self.assertRaisesRegex(core.ValidationError, "can move to"):
            core.update_status(self.conn, 1, "connected", "they accepted")

    def test_full_path_and_final(self):
        for s in ("qualified", "sent", "pending", "connected"):
            core.update_status(self.conn, 1, s, f"seen {s}", "https://www.linkedin.com/mynetwork/")
        with self.assertRaisesRegex(core.ValidationError, "final"):
            core.update_status(self.conn, 1, "research", "x")

    def test_invitation_rejection_separate_from_research_exclusion(self):
        with self.assertRaisesRegex(core.ValidationError, "can move to"):  # research says no via 'excluded'
            core.update_status(self.conn, 1, "rejected", "not a fit")
        core.update_status(self.conn, 1, "excluded", "US-only role")
        core.update_status(self.conn, 1, "research", "company now hires in Europe")
        core.update_status(self.conn, 1, "qualified", "fits policy")
        core.update_status(self.conn, 1, "sent", "sent")
        core.update_status(self.conn, 1, "rejected", "invitation withdrawn after 3 weeks")
        self.assertEqual(core.find_lead(self.conn, P)["status"], "rejected")

    def test_events_immutable(self):
        core.update_status(self.conn, 1, "qualified", "fits")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE events SET evidence = 'x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM events")


class Queue(DBTest):
    def test_ready_lead(self):
        core.upsert_leads(self.conn, ready_lead(), SETTINGS)
        self.assertEqual([r["url"] for r in core.queue(self.conn, SETTINGS)["ready"]], [P])

    def blockers(self, **over):
        core.upsert_leads(self.conn, ready_lead(**over), SETTINGS)
        return core.queue_blockers(core.find_lead(self.conn, P), SETTINGS)

    def test_excluded_country_by_location(self):
        self.assertTrue(any("excluded" in b for b in self.blockers(contact_country="Exampleland")))

    def test_excluded_country_codes_accents_cities(self):
        for country, location in [("EX", None), ("EXL", None), ("Exámpleland", None),
                                  (None, "Sample City"), ("United Kingdom", "Exampleland")]:
            self.assertIsNotNone(core.excluded_location(country, location, SETTINGS), (country, location))
        for country in ("Republic of Exampleland", "Exampleland (remote)"):
            self.assertEqual(core.excluded_location(country, None, SETTINGS), "Exampleland", country)
        for country, location in [("United Kingdom", "London"), ("Netherlands", "Amsterdam"),
                                  ("Germany", "Sample City"), ("Indonesia", "Jakarta")]:
            self.assertIsNone(core.excluded_location(country, location, SETTINGS), (country, location))
        self.assertTrue(any("excluded" in b for b in self.blockers(contact_country="EX")))

    def test_unknown_country_blocks(self):
        core.upsert_leads(self.conn, ready_lead(linkedin_url="https://www.linkedin.com/in/nocountry",
                                                contact_country=None), SETTINGS)
        lead = core.find_lead(self.conn, "https://www.linkedin.com/in/nocountry")
        self.assertIn("contact country unknown: check the profile location", core.queue_blockers(lead, SETTINGS))

    def test_restricted_roles_and_vendors(self):
        self.assertTrue(any("restricted" in b for b in self.blockers(eligibility="us_only")))
        core.edit_lead(self.conn, 1, {"eligibility": "worldwide", "company_type": "low_cost_staff_augmentation"}, SETTINGS)
        lead = core.find_lead(self.conn, P)
        self.assertTrue(any("skipped by policy" in b for b in core.queue_blockers(lead, SETTINGS)))

    def test_eligibility_values(self):
        for elig in ("uk_only", "other_restricted"):
            self.assertTrue(any("restricted" in b for b in self.blockers(eligibility=elig)), elig)
        self.assertEqual(self.blockers(eligibility="europe_including_romania"), [])
        core.edit_lead(self.conn, 1, {"eligibility": None}, SETTINGS)
        lead = core.find_lead(self.conn, P)
        self.assertEqual(lead["eligibility"], "unknown")  # clearing an enum stores 'unknown'
        self.assertTrue(any("unverified" in b for b in core.queue_blockers(lead, SETTINGS)))

    def test_hold_and_missing_draft(self):
        b = self.blockers(hold_reason="wait for reply from recruiter", invitation_draft=None)
        self.assertTrue(any(x.startswith("on hold") for x in b))
        self.assertIn("no invitation draft", b)
        core.edit_lead(self.conn, 1, {"hold_reason": None, "invitation_draft": "Hi Jane"}, SETTINGS)
        self.assertEqual(core.queue_blockers(core.find_lead(self.conn, P), SETTINGS), [])

    def test_draft_over_current_limit_blocks(self):
        core.upsert_leads(self.conn, ready_lead(invitation_draft="x" * 250), SETTINGS)
        tighter = dict(SETTINGS, invitation={"max_note_chars": 200})
        self.assertIn("invitation draft longer than 200 characters",
                      core.queue_blockers(core.find_lead(self.conn, P), tighter))

    def test_non_qualified_not_in_queue(self):
        core.upsert_leads(self.conn, ready_lead(status="research"), SETTINGS)
        self.assertEqual(core.queue(self.conn, SETTINGS)["ready"], [])

    def test_budget_never_inferred(self):
        core.upsert_leads(self.conn, ready_lead(), SETTINGS)
        self.assertEqual(core.budget_view(core.find_lead(self.conn, P), SETTINGS)["text"], "unknown")
        src = "https://acme.example/job/1"
        core.upsert_leads(self.conn, ready_lead(budget_min=60, budget_max=80, budget_currency="GBP",
                                                budget_period="hour", budget_source_url=src), SETTINGS)
        view = core.budget_view(core.find_lead(self.conn, P), SETTINGS)
        self.assertEqual(view["meets_goal"], None)  # no currency/period conversion
        self.assertIn("GBP per hour", view["text"])
        core.upsert_leads(self.conn, ready_lead(budget_min=6000, budget_max=8000, budget_currency="EUR",
                                                budget_period="month"), SETTINGS)
        self.assertTrue(core.budget_view(core.find_lead(self.conn, P), SETTINGS)["meets_goal"])
        core.upsert_leads(self.conn, ready_lead(budget_min=5000, budget_max=6000), SETTINGS)
        self.assertFalse(core.budget_view(core.find_lead(self.conn, P), SETTINGS)["meets_goal"])


class Applications(DBTest):
    def rejection(self, **over):
        app = {"company": "Acme Ltd", "role_title": "Senior Engineer", "applied_date": "2026-09-01",
               "outcome": "rejected", "rejection_date": "2026-09-02", "rejection_source": "automated_email",
               "decision_basis": "automated_email_only", "evidence": "Generic no-reply email two days later"}
        app.update(over)
        return app

    def test_provenance_rules(self):
        with self.assertRaisesRegex(core.ValidationError, "evidence"):
            core.upsert_applications(self.conn, self.rejection(evidence=None))
        with self.assertRaisesRegex(core.ValidationError, "human_screening_confirmed"):
            core.upsert_applications(self.conn, self.rejection(decision_basis="human_screening_confirmed"))
        with self.assertRaisesRegex(core.ValidationError, "automated_email_only"):
            core.upsert_applications(self.conn, self.rejection(rejection_source="recruiter_message"))
        ats = {"rejection_source": "ats_status", "decision_basis": "ats_auto_reject_proven"}
        with self.assertRaisesRegex(core.ValidationError, "evidence"):
            core.upsert_applications(self.conn, self.rejection(evidence=None, evidence_url="https://x.example", **ats))
        with self.assertRaisesRegex(core.ValidationError, "ats_auto_reject_proven"):
            core.upsert_applications(self.conn, self.rejection(**dict(ats, rejection_source="recruiter_message")))
        core.upsert_applications(self.conn, self.rejection(evidence="Portal: rejected by knockout question", **ats))

    def test_revisit_list_and_company_link(self):
        core.upsert_applications(self.conn, [
            self.rejection(),
            self.rejection(company="Beta GmbH", rejection_source="recruiter_message",
                           decision_basis="human_screening_confirmed", evidence="Recruiter said profile not a fit"),
        ])
        core.upsert_leads(self.conn, {"linkedin_url": P, "company": "ACME"}, SETTINGS)
        revisit = core.list_applications(self.conn, revisit_only=True, settings=SETTINGS)
        self.assertEqual([a["company"] for a in revisit], ["Acme Ltd"])
        self.assertEqual(revisit[0]["lead_count"], 1)
        self.assertEqual(len(core.company_applications(self.conn, core.find_lead(self.conn, P)["company_key"])), 1)
        core.set_revisit_status(self.conn, revisit[0]["id"], "done")
        self.assertEqual(core.list_applications(self.conn, revisit_only=True, settings=SETTINGS), [])

    def test_upsert_is_idempotent(self):
        core.upsert_applications(self.conn, self.rejection())
        res = core.upsert_applications(self.conn, self.rejection(company="ACME LIMITED"))
        self.assertEqual(res[0]["action"], "updated")
        self.assertEqual(len(core.list_applications(self.conn)), 1)


class Runs(DBTest):
    def test_record_and_validate(self):
        core.record_run(self.conn, {"kind": "research", "summary": "Checked 12 UK fintechs", "counts": {"added": 3}})
        self.assertEqual(core.list_runs(self.conn)[0]["counts"], {"added": 3})
        with self.assertRaises(core.ValidationError):
            core.record_run(self.conn, {"kind": "x", "summary": "y", "counts": {"added": "three"}})


if __name__ == "__main__":
    unittest.main()
