import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from foia_requests.ledger import Ledger, RequestScope
from foia_requests.templates import (TEMPLATE_DIR, RequesterProfile, TemplateError, load_template, render,
                                     store)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "fixtures/profiles/synthetic-requester.json").read_text())
COMPONENT = {"component_id": "fed:doj/oip", "name": "Office of Information Policy",
             "agency_name": "Department of Justice"}
ROUTE = {"route_id": "route:fed:doj/oip:email:1", "component_id": "fed:doj/oip", "kind": "email",
         "destination": "foia-test@example.invalid", "state": "verified"}


def scope(start="2023-10-01", end="2024-09-30", basis="fiscal"):
    return RequestScope("requester:synthetic-1", "us-federal", "fed:doj/oip", "foia_request_log", start, end, basis)


def profile(**changes):
    data = copy.deepcopy(PROFILE)
    data.update(changes)
    return RequesterProfile.from_dict(data)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template()

    def test_golden_output_is_exact_and_deterministic(self):
        first = render(self.template, scope(), COMPONENT, ROUTE, profile())
        second = render(self.template, scope(), dict(COMPONENT), dict(ROUTE), profile())
        golden = (ROOT / "tests/golden/annual_foia_log_request_v1_fiscal.txt").read_text(encoding="utf-8")
        self.assertEqual(first.subject + "\n\n" + first.body, golden)
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertIn("October 1, 2023 through September 30, 2024", first.body)
        self.assertIn("Office of Information Policy", first.subject)

    def test_leap_day_and_calendar_scope_preserved(self):
        rendered = render(self.template, scope("2024-02-29", "2024-12-31", "custom"), COMPONENT, ROUTE, profile())
        self.assertIn("February 29, 2024 through December 31, 2024", rendered.body)
        self.assertEqual(rendered.manifest["period"], ["2024-02-29", "2024-12-31", "custom"])

    def test_synthetic_or_incomplete_inputs_never_live_ready(self):
        rendered = render(self.template, scope(), COMPONENT, ROUTE, profile())
        self.assertFalse(rendered.live_ready)
        self.assertIn("real requester profile (fixture profile is synthetic)", rendered.missing_inputs)
        cases = {
            "requester name": profile(name=None),
            "fee policy": profile(fee_policy=None),
            "requester reply route": profile(reply_email=None),
            "verified requester identity": profile(synthetic=False, verified=False),
        }
        for missing, prof in cases.items():
            with self.subTest(missing=missing):
                out = render(self.template, scope(), COMPONENT, ROUTE, prof)
                self.assertIn(missing, out.missing_inputs)
                self.assertFalse(out.live_ready)
                if missing in ("requester name", "fee policy"):
                    self.assertIn(f"[MISSING: {missing}]", out.body)
        no_route = render(self.template, scope(), COMPONENT, None, profile())
        self.assertIn("official submission route", no_route.missing_inputs)
        stale = render(self.template, scope(), COMPONENT, dict(ROUTE, state="stale"), profile())
        self.assertTrue(any("verified submission route" in m for m in stale.missing_inputs))

    def test_unapproved_template_blocks_live_ready_even_with_complete_inputs(self):
        real = profile(synthetic=False, verified=True)
        out = render(self.template, scope(), COMPONENT, ROUTE, real)
        self.assertEqual(out.missing_inputs,
                         ("approved template (status: engineering_draft_pending_legal_review)",))

    def test_no_automatic_payment_promise_waiver_or_private_requester_fields(self):
        out = render(self.template, scope(), COMPONENT, ROUTE, profile())
        self.assertNotIn("agree to pay fees up to", out.body)
        self.assertNotIn("fee waiver", out.body)
        self.assertNotIn("expedit", out.body.lower())
        self.assertIn("does not seek the names", out.body)
        paid = render(self.template, scope(), COMPONENT, ROUTE,
                      profile(fee_policy={"max_authorized_usd": 25, "fee_waiver_basis": None}))
        self.assertIn("I agree to pay fees up to $25.", paid.body)
        waiver = render(self.template, scope(), COMPONENT, ROUTE,
                        profile(fee_policy={"max_authorized_usd": 0, "fee_waiver_basis": "synthetic basis"}))
        self.assertIn("fee waiver on the following basis: synthetic basis", waiver.body)

    def test_injection_and_bounds_rejected(self):
        for bad in ("Office\r\nBcc: x@example.invalid", "Office‮", "x" * 201, "Office\x00"):
            with self.subTest(bad=bad[:20]), self.assertRaises(TemplateError):
                render(self.template, scope(), dict(COMPONENT, name=bad), ROUTE, profile())
        with self.assertRaises(TemplateError):
            profile(name="Name\nInjected")
        with self.assertRaises(TemplateError):
            profile(fee_policy={"max_authorized_usd": 1.5, "fee_waiver_basis": None})
        with self.assertRaises(TemplateError):
            RequesterProfile.from_dict(dict(PROFILE, extra=True))

    def test_mismatched_inputs_rejected(self):
        with self.assertRaises(TemplateError):
            render(self.template, scope(), dict(COMPONENT, component_id="fed:doj/fbi"), ROUTE, profile())
        with self.assertRaises(TemplateError):
            render(self.template, scope(), COMPONENT, dict(ROUTE, component_id="fed:doj/fbi"), profile())
        with self.assertRaises(TemplateError):
            render(self.template, scope(), COMPONENT, ROUTE, profile(requester_principal="requester:other"))

    def test_template_edit_without_new_revision_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            copy_dir = Path(folder) / "templates"
            shutil.copytree(TEMPLATE_DIR, copy_dir)
            path = copy_dir / "annual_foia_log_request.v1.json"
            data = json.loads(path.read_text())
            data["body"] = data["body"].replace("existing", "all")
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(TemplateError, "LOCK"):
                load_template(directory=copy_dir)
            data["body"] += "${unknown_placeholder}"
            path.write_text(json.dumps(data))
            with self.assertRaises((TemplateError, KeyError)):
                load_template(directory=copy_dir)

    def test_template_revision_creates_ledger_revision_not_new_request(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                rendered = render(self.template, scope(), COMPONENT, ROUTE, profile())
                rid, number, created = store(ledger, rendered)
                self.assertEqual((number, created), (1, True))
                self.assertEqual(store(ledger, rendered), (rid, 1, False))
                alternate = render(self.template, scope(), COMPONENT, ROUTE,
                                   profile(delivery_format="PDF only"))
                rid2, number2, _ = store(ledger, alternate)
                self.assertEqual((rid2, number2), (rid, 2))
                self.assertEqual(len(ledger.list_requests()), 1)
                self.assertEqual(ledger.latest_revision(rid)["content_sha256"], alternate.content_sha256)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
