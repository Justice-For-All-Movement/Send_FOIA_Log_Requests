import copy
import json
import os
import unittest
from pathlib import Path

from foia_requests.ledger import RequestScope
from foia_requests.release_gaps import load_gaps, render_followup
from foia_requests.templates import (FEE_CATEGORIES, RequesterProfile, TemplateError, load_active_template,
                                     load_template, render)

ROOT = Path(__file__).resolve().parents[1]
JOURNALIST = json.loads((ROOT / "fixtures/profiles/synthetic-journalist.json").read_text())
BASIC = json.loads((ROOT / "fixtures/profiles/synthetic-requester.json").read_text())
COMPONENT = {"component_id": "fed:doj/oip", "name": "Office of Information Policy",
             "agency_name": "Department of Justice"}
ROUTE = {"route_id": "route:fed:doj/oip:email:1", "component_id": "fed:doj/oip", "kind": "email",
         "destination": "foia-test@example.invalid", "state": "verified"}
REGENERATE = os.environ.get("FOIA_REGENERATE_GOLDEN") == "1"


def profile(base=JOURNALIST, **changes):
    data = copy.deepcopy(base)
    data.update(changes)
    return RequesterProfile.from_dict(data)


def scope(principal="requester:synthetic-journalist-1", start="2026-10-01", end="2026-10-31"):
    return RequestScope(principal, "us-federal", "fed:doj/oip", "foia_closed_request_log", start, end, "custom")


def golden(name, text):
    path = ROOT / "tests/golden" / name
    if REGENERATE:
        path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8")


class PeriodicTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template("periodic_closed_request_log", "1")

    def test_golden_text_and_status(self):
        rendered = render(self.template, scope(), COMPONENT, ROUTE, profile())
        text = rendered.subject + "\n\n" + rendered.body
        self.assertEqual(text, golden("periodic_closed_request_log_v1_monthly.txt", text))
        self.assertEqual(self.template.status, "engineering_draft_pending_legal_review")
        self.assertEqual(render(self.template, scope(), dict(COMPONENT), dict(ROUTE), profile()).content_sha256,
                         rendered.content_sha256)

    def test_required_content(self):
        body = render(self.template, scope(), COMPONENT, ROUTE, profile()).body
        self.assertIn("FOIA requests closed between October 1, 2026 and October 31, 2026", body)
        self.assertNotIn("received from", body)
        self.assertIn("5 U.S.C. 552(a)(3)(B)", body)
        self.assertIn("spreadsheet or CSV export", body)
        self.assertIn("request tracking number; date received; date closed; subject or description of the request "
                      "as logged; final disposition; number of pages released. No other fields", body)
        self.assertEqual(len(self.template.requested_fields), 6)
        self.assertIn("Requester names, addresses and other contact details are not needed and may be omitted", body)
        self.assertIn("does not ask the agency to create records", body)
        self.assertIn("only for an export of existing log data", body)
        self.assertIn("5 U.S.C. 552(a)(4)(A)(iii)", body)
        self.assertIn("contribute significantly to public understanding of the operations or activities of the "
                      "government", body)
        self.assertIn("not primarily in my commercial interest", body)
        self.assertIn("I will make the information available to the public as follows: [TEST FIXTURE]", body)
        # News media status: the three statutory elements, with outlet facts from the profile.
        for phrase in ("representative of the news media", "552(a)(4)(A)(ii)(II)",
                       "gather information of potential interest to a segment of the public",
                       "editorial skills to turn the raw materials into a distinct work",
                       "distribute that work to an audience", "Example Test Newsletter (https://news.example.invalid)"):
            self.assertIn(phrase, body)

    def test_fee_category_comes_from_profile(self):
        bodies = {}
        for category in FEE_CATEGORIES:
            bodies[category] = render(self.template, scope(), COMPONENT, ROUTE, profile(fee_category=category)).body
        self.assertIn("all other\" requester under 5 U.S.C. 552(a)(4)(A)(ii)(III)", bodies["other"])
        self.assertIn("educational institution fee category", bodies["educational"])
        self.assertNotIn("news media", bodies["other"])
        self.assertEqual(len(set(bodies.values())), 3)
        for body in bodies.values():
            self.assertIn("552(a)(4)(A)(iii)", body)  # waiver present for every category

    def test_missing_category_and_facts_are_visible_markers(self):
        rendered = render(self.template, scope("requester:synthetic-1"), COMPONENT, ROUTE, profile(BASIC))
        self.assertIn("[MISSING: fee category", rendered.body)
        self.assertIn("[MISSING: dissemination plan for the fee waiver]", rendered.body)
        self.assertFalse(rendered.live_ready)
        rendered = render(self.template, scope(), COMPONENT, ROUTE, profile(publication=None))
        self.assertIn("[MISSING: publication name]", rendered.body)
        self.assertIn("publication facts (name, url, audience) supporting fee category news_media",
                      rendered.missing_inputs)

    def test_no_unsupported_fee_grounds(self):
        data = (ROOT / "foia_requests/templates/periodic_closed_request_log.v1.json").read_text().lower()
        data += (ROOT / "foia_requests/templates/released_records_followup.v1.json").read_text().lower()
        for word in ("politician", "elected", "low-income", "low income", "indigent", "poverty"):
            self.assertNotIn(word, data)
        with self.assertRaisesRegex(TemplateError, "profile contract"):
            profile(political_office="mayor")
        with self.assertRaisesRegex(TemplateError, "fee_category"):
            profile(fee_category="low_income")
        with self.assertRaisesRegex(TemplateError, "https"):
            profile(publication={"name": "x", "url": "http://x.example", "audience": "y"})
        with self.assertRaisesRegex(TemplateError, "publication must contain"):
            profile(publication={"name": "x", "url": "https://x.example"})

    def test_record_class_and_annual_template_unchanged(self):
        self.assertEqual(self.template.record_class, "foia_closed_request_log")
        annual = load_active_template()
        self.assertEqual(annual.key, "annual_foia_log_request@1")
        with self.assertRaisesRegex(TemplateError, "jurisdiction/record class"):
            render(annual, scope(), COMPONENT, ROUTE, profile())
        # Existing profiles without the optional fields still load.
        self.assertIsNone(profile(BASIC).fee_category)

    def test_lock_pins_new_templates(self):
        lock = json.loads((ROOT / "foia_requests/templates/LOCK.json").read_text())
        self.assertIn("periodic_closed_request_log@1", lock)
        self.assertIn("released_records_followup@1", lock)
        self.assertEqual(load_active_template("periodic_closed_request_log").key, "periodic_closed_request_log@1")


class FollowupTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template("released_records_followup", "1")
        self.gaps = load_gaps(ROOT / "fixtures/release_gaps").gaps

    def test_golden_single(self):
        letter = render_followup(self.template, [self.gaps[0]], agency_name="Department of Justice",
                                 addressee="Office of Information Policy (Department of Justice)", route=ROUTE,
                                 profile=profile())
        text = letter["subject"] + "\n\n" + letter["body"]
        self.assertEqual(text, golden("released_records_followup_v1_single.txt", text))
        self.assertIn("a copy of the records released in response to FOIA request DOJ-2026-000101 (closed June 15, "
                      "2026, subject as logged: SYNTHETIC: training materials on FOIA processing)", letter["body"])
        self.assertIn("electronically", letter["body"])
        self.assertIn("already been posted publicly", letter["body"])
        self.assertIn("a link to them is sufficient", letter["body"])
        self.assertIn("552(a)(4)(A)(iii)", letter["body"])
        self.assertEqual(self.template.status, "engineering_draft_pending_legal_review")

    def test_same_waiver_paragraph_as_periodic(self):
        periodic = render(load_template("periodic_closed_request_log", "1"), scope(), COMPONENT, ROUTE, profile())
        letter = render_followup(self.template, [self.gaps[0]], agency_name="Department of Justice",
                                 addressee="OIP", route=ROUTE, profile=profile())

        def waiver(body):
            return next(p for p in body.split("\n\n") if p.startswith("Fee waiver:"))
        self.assertEqual(waiver(periodic.body), waiver(letter["body"]))

    def test_nulls_and_group_label(self):
        letter = render_followup(self.template, list(self.gaps[:2]) + [self.gaps[4]], agency_name="X",
                                 addressee="X", route=None, profile=profile())
        self.assertIn("3 closed FOIA requests (DOJ-2026-000101 and others)", letter["subject"])
        self.assertIn("closed date not given in the log", letter["body"])
        self.assertIn("official submission route", letter["missing_inputs"])
        with self.assertRaisesRegex(TemplateError, "1..25"):
            render_followup(self.template, [self.gaps[0]] * 26, agency_name="X", addressee="X", route=None,
                            profile=profile())


if __name__ == "__main__":
    unittest.main()
