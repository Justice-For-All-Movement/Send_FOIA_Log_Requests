"""FOIA.gov converter tests over a synthetic JSON:API page (made-up components; nothing is sent)."""
import json
import tempfile
import unittest
from pathlib import Path

from foia_requests import directory
from foia_requests.foia_gov import ConversionError, convert, convert_pages
from foia_requests.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "foia_gov_directory"


def raw_page(url, data, included, next_url=None, fetched_at="2026-11-16T00:00:00Z"):
    links = {"next": {"href": next_url}} if next_url else {}
    return {"fetched_url": url, "fetched_at": fetched_at, "status": 200,
            "body": {"data": data, "included": included, "links": links}}


AGENCY = {"type": "agency", "id": "a-1", "attributes": {"name": "Department of Examples", "abbreviation": "DOE-X"}}


def component(cid, title, abbr, **attrs):
    return {"type": "agency_component", "id": cid, "attributes": dict(title=title, abbreviation=abbr, **attrs),
            "relationships": {"agency": {"data": {"type": "agency", "id": "a-1"}}}}


class FoiaGovConversionTests(unittest.TestCase):
    def pages(self):
        first = raw_page("https://api.foia.gov/api/agency_components?include=agency&page%5Blimit%5D=50", [
            component("c-1", "Office of Samples", "OS", email=["foia@samples.example-agency.gov"],
                      submission_web={"uri": "https://foia.example-agency.gov/request"},
                      submission_address={"address_line1": "1 Sample Way", "locality": "Washington",
                                          "administrative_area": "DC", "postal_code": "20500"}),
            component("c-2", "Second Office", "OS", email="Contact: second@example-agency.com"),
        ], [AGENCY], next_url="https://api.foia.gov/api/agency_components?page%5Boffset%5D=50")
        second = raw_page("https://api.foia.gov/api/agency_components?page%5Boffset%5D=50", [
            component("c-3", "Retired Office", "RO", status=False),
            component("c-4", "Third Office", None, email=None),
        ], [AGENCY], fetched_at="2026-11-16T00:01:00Z")
        return [first, second]

    def test_convert_and_discover(self):
        with tempfile.TemporaryDirectory() as folder:
            raw, out = Path(folder) / "raw", Path(folder) / "out"
            raw.mkdir()
            for i, page in enumerate(self.pages(), 1):
                (raw / f"page-{i:04d}.json").write_text(json.dumps(page))
            summary = convert(raw, out)
            self.assertEqual((summary["pages"], summary["components"]), (2, 3))
            result = directory.discover(directory.load_source(out), directory.fixture_fetcher(out))
            self.assertTrue(result.complete)
            comps = result.components
            self.assertEqual(sorted(comps), ["fed:doe-x/os", "fed:doe-x/os-" + sorted(comps)[1][-6:],
                                             "fed:doe-x/third-office"])
            states = {(r.kind, r.state) for r in comps["fed:doe-x/os"].routes.values()}
            self.assertEqual(states, {("email", "verified"), ("portal", "verified"), ("postal", "manual")})
            other = next(c for k, c in comps.items() if k.startswith("fed:doe-x/os-"))
            self.assertEqual({r.state for r in other.routes.values()}, {"unverified"})
            with Ledger(Path(folder) / "state" / "ledger.sqlite") as ledger:
                self.assertTrue(directory.persist(ledger, result))

    def test_failed_page_is_rejected(self):
        page = self.pages()[0]
        page["status"] = 429
        with self.assertRaises(ConversionError):
            convert_pages([page])


@unittest.skipUnless((SNAPSHOT / "source.json").exists(), "no committed FOIA.gov snapshot")
class CommittedSnapshotTests(unittest.TestCase):
    def test_snapshot_discovers_completely(self):
        result = directory.discover(directory.load_source(SNAPSHOT), directory.fixture_fetcher(SNAPSHOT))
        self.assertTrue(result.complete)
        self.assertGreater(len(result.components), 50)
        verified = [r for c in result.components.values() for r in c.routes.values() if r.state == "verified"]
        self.assertGreater(len(verified), 50)


if __name__ == "__main__":
    unittest.main()
