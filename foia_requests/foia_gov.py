"""Convert raw FOIA.gov agency-component pages into a directory snapshot.

Input: the raw pages written by ``scripts/fetch_foia_gov_directory.py``
(JSON:API documents from https://api.foia.gov/api/agency_components with the
``agency`` relationship included). Output: a folder that
``foia-requests directory --source`` reads (``source.json`` plus
``page-NNNN.json``), so routes go through the same validation as any other
directory: e-mail domains and portal hosts must be official (.gov/.mil) to
be ``verified``; anything else stays ``unverified``/``invalid`` and is never
sent to. Postal addresses become manual routes.

This is public agency contact data published by FOIA.gov. Run:

    python -m foia_requests.foia_gov --raw RAW --out data/foia_gov_directory
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SOURCE_ID = "foia.gov-agency-components"
API_HOST = "api.foia.gov"
COMPONENT_URL = "https://api.foia.gov/api/agency_components/{id}"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")


class ConversionError(ValueError):
    pass


def _text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("value", "processed", "uri", "url"):
            if isinstance(value.get(key), str):
                return value[key].strip()
    return ""


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _emails(attributes):
    found = []
    for key in ("email", "emails", "submission_email"):
        for item in _list(attributes.get(key)):
            for match in EMAIL.findall(_text(item)):
                if match.lower() not in (e.lower() for e in found):
                    found.append(match)
    return found


def _urls(attributes, keys):
    found = []
    for key in keys:
        for item in _list(attributes.get(key)):
            url = _text(item)
            if url.startswith("http") and url not in found:
                found.append(url)
    return found


def _postal(attributes):
    address = attributes.get("submission_address")
    if not isinstance(address, dict):
        return None
    parts = [address.get(k) for k in ("organization", "address_line1", "address_line2", "locality",
                                      "administrative_area", "postal_code")]
    text = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return text or None


def _abbr(value, fallback):
    text = (value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text[:48] or "unknown"


def convert_pages(raw_pages):
    """``raw_pages``: list of fetch records in order. Returns (source, [page dicts])."""
    agencies = {}
    for record in raw_pages:
        if record.get("status") != 200:
            raise ConversionError(f"raw page {record.get('fetched_url')} has status {record.get('status')}")
        for item in record["body"].get("included") or []:
            if item.get("type", "").endswith("agency"):
                agencies[item["id"]] = item.get("attributes") or {}
    used = {}
    pages = []
    observed = max(r["fetched_at"] for r in raw_pages)
    for index, record in enumerate(raw_pages, 1):
        items = []
        for comp in record["body"].get("data") or []:
            attrs = comp.get("attributes") or {}
            if attrs.get("status") is False:
                continue
            rel = ((comp.get("relationships") or {}).get("agency") or {}).get("data") or {}
            agency = agencies.get(rel.get("id"), {})
            agency_name = (agency.get("name") or agency.get("title") or "").strip() or "Unknown agency"
            agency_abbr = _abbr(agency.get("abbreviation"), agency_name)
            name = (attrs.get("title") or attrs.get("name") or "").strip()
            if not name:
                continue
            component_abbr = _abbr(attrs.get("abbreviation"), name)
            key = (agency_abbr.lower(), component_abbr.lower())
            if key in used and used[key] != comp["id"]:
                component_abbr = f"{component_abbr}-{hashlib.sha256(comp['id'].encode()).hexdigest()[:6]}"
            used[(agency_abbr.lower(), component_abbr.lower())] = comp["id"]
            evidence = COMPONENT_URL.format(id=comp["id"])
            routes = [{"kind": "email", "destination": e, "source_url": evidence} for e in _emails(attrs)]
            routes += [{"kind": "portal", "destination": u, "source_url": evidence}
                       for u in _urls(attrs, ("submission_web", "request_form_url"))]
            postal = _postal(attrs)
            if postal:
                routes.append({"kind": "postal", "destination": postal, "source_url": evidence})
            items.append({"agency_abbr": agency_abbr, "agency_name": agency_name, "component_abbr": component_abbr,
                          "component_name": name, "status": "current", "aliases": [], "routes": routes,
                          "foia_gov_id": comp["id"],
                          "website": next(iter(_urls(attrs, ("website",))), None)})
        pages.append({"status": 200, "url": record["fetched_url"],
                      "next": f"page-{index + 1:04d}.json" if index < len(raw_pages) else None, "items": items})
    source = {"source_id": SOURCE_ID,
              "description": "Official FOIA.gov agency components (public contact data published by FOIA.gov, "
                             "api.foia.gov). Converted by foia_requests.foia_gov.",
              "official_hosts": [API_HOST], "first_page": "page-0001.json", "observed_at": observed,
              "official_suffixes": [".gov", ".mil"],
              "upstream": "https://api.foia.gov/api/agency_components?include=agency"}
    return source, pages


def convert(raw_dir, out_dir):
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    raw = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(raw_dir.glob("page-*.json"))]
    if not raw:
        raise ConversionError(f"no raw pages in {raw_dir}")
    source, pages = convert_pages(raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page-*.json"):
        old.unlink()
    (out_dir / "source.json").write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for index, page in enumerate(pages, 1):
        (out_dir / f"page-{index:04d}.json").write_text(
            json.dumps(page, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"pages": len(pages), "components": sum(len(p["items"]) for p in pages),
            "routes": sum(len(i["routes"]) for p in pages for i in p["items"]), "observed_at": source["observed_at"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m foia_requests.foia_gov")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(convert(args.raw, args.out), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
