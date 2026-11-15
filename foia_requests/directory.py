"""SFL-02 official agency/component and contact-route discovery (offline).

Discovery reads directory *pages* through an injected ``fetch_page`` callable.
This module ships only a local-fixture fetcher; a live adapter must be added
separately after the official endpoint, authentication and schema are verified
(see docs). Nothing here performs network I/O.

Guarantees
* Stable namespaced IDs ``<jurisdiction>:<agency>/<component>`` derived from
  directory abbreviations, never from display names, so equal names in
  different agencies stay distinct and renames keep their identity via aliases.
* Routes are validated: e-mail/portal destinations must be on an official
  suffix; HTTPS only, no credentials, no IP literals, no non-default ports;
  any redirect hop leaving the official namespace makes the route invalid.
  Off-namespace contacts are kept as ``unverified`` (never used for sending).
  Postal routes are ``manual``.
* Pagination is bounded and loop-checked. 403/429/5xx pages stop discovery
  with an actionable queue entry (including Retry-After) and ``complete=False``.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .ledger import canonical_json

DEFAULT_OFFICIAL_SUFFIXES = (".gov", ".mil")
ROUTE_KINDS = ("email", "portal", "postal")
COMPONENT_STATUSES = ("current", "retired", "renamed", "unknown")
MAX_PAGES = 500
MAX_PAGE_BYTES = 4 * 1024 * 1024
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")


class DirectoryError(ValueError):
    pass


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not text or len(text) > 64:
        raise DirectoryError(f"cannot derive a stable identifier from {value!r}")
    return text


def component_id(jurisdiction: str, agency_abbr: str, component_abbr: str) -> str:
    return f"{jurisdiction}:{slug(agency_abbr)}/{slug(component_abbr)}"


def official_host(host: str | None, suffixes) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(host == s.lstrip(".") or host.endswith(s) for s in suffixes)


def check_url(url: str, suffixes):
    """Return None if ``url`` is a safe official HTTPS URL, else a reason."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "malformed URL"
    if parts.scheme != "https":
        return "non-HTTPS URL"
    if parts.username or parts.password:
        return "credentials embedded in URL"
    host = parts.hostname
    try:
        ipaddress.ip_address(host or "")
        return "IP-literal host"
    except ValueError:
        pass
    try:
        if parts.port not in (None, 443):
            return "non-default port"
    except ValueError:
        return "malformed port"
    if not official_host(host, suffixes):
        return f"host {host} is outside the official namespace"
    return None


@dataclass(frozen=True)
class Route:
    route_id: str
    component_id: str
    kind: str
    destination: str
    source_url: str
    state: str  # verified | unverified | invalid | manual | stale
    reason: str
    redirects: tuple = ()
    verified_at: str | None = None

    def as_dict(self):
        return {"route_id": self.route_id, "component_id": self.component_id, "kind": self.kind,
                "destination": self.destination, "source_url": self.source_url, "state": self.state,
                "reason": self.reason, "redirects": list(self.redirects), "verified_at": self.verified_at}


@dataclass
class Component:
    component_id: str
    agency_id: str
    agency_name: str
    name: str
    status: str
    aliases: set = field(default_factory=set)
    renamed_to: str | None = None
    routes: dict = field(default_factory=dict)

    def as_dict(self):
        return {"component_id": self.component_id, "agency_id": self.agency_id, "agency_name": self.agency_name,
                "name": self.name, "status": self.status, "aliases": sorted(self.aliases),
                "renamed_to": self.renamed_to,
                "routes": [self.routes[k].as_dict() for k in sorted(self.routes)]}


@dataclass
class DiscoveryResult:
    source_id: str
    observed_at: str
    snapshot_sha256: str
    complete: bool
    pages: int
    components: dict
    actions: list

    def as_dict(self):
        return {"source_id": self.source_id, "observed_at": self.observed_at,
                "snapshot_sha256": self.snapshot_sha256, "complete": self.complete, "pages": self.pages,
                "components": [self.components[k].as_dict() for k in sorted(self.components)],
                "actions": self.actions}


def build_route(cid, raw, suffixes, source_hosts, observed_at):
    kind = raw.get("kind")
    destination = raw.get("destination")
    source_url = raw.get("source_url")
    redirects = tuple(raw.get("redirects", ()))
    if kind not in ROUTE_KINDS or not isinstance(destination, str) or not isinstance(source_url, str):
        raise DirectoryError("route requires kind, destination and source_url")
    digest = hashlib.sha256(canonical_json([cid, kind, destination]).encode()).hexdigest()[:16]
    route_id = f"route:{cid}:{kind}:{digest}"
    source_problem = check_url(source_url, suffixes)
    if source_problem is None and urlsplit(source_url).hostname not in source_hosts:
        source_problem = "evidence URL is not the directory source"
    state, reason = "verified", "official directory listing"
    if kind == "postal":
        state, reason = "manual", "postal routes are manual tasks"
    elif kind == "email":
        match = EMAIL.fullmatch(destination)
        if match is None:
            state, reason = "invalid", "malformed e-mail address"
        elif not official_host(match.group(1), suffixes):
            state, reason = "unverified", "e-mail domain outside the official namespace"
    else:
        problem = check_url(destination, suffixes)
        if problem:
            state, reason = "invalid", problem
        else:
            for hop in redirects:
                hop_problem = check_url(hop, suffixes)
                if hop_problem:
                    state, reason = "invalid", f"unsafe redirect: {hop_problem}"
                    break
    if state == "verified" and source_problem:
        state, reason = "unverified", source_problem
    return Route(route_id, cid, kind, destination, source_url, state, reason, redirects,
                 observed_at if state == "verified" else None)


def fixture_fetcher(directory):
    """Return a fetch_page(name) callable reading bounded local fixture pages."""
    root = Path(directory).resolve()

    def fetch(name):
        if "/" in name or "\\" in name or name.startswith("."):
            raise DirectoryError("fixture page names must be plain file names")
        path = (root / name)
        raw = path.read_bytes()
        if len(raw) > MAX_PAGE_BYTES:
            raise DirectoryError("directory page exceeds size bound")
        return json.loads(raw.decode("utf-8"))
    return fetch


def load_source(directory):
    data = json.loads((Path(directory) / "source.json").read_text(encoding="utf-8"))
    for key in ("source_id", "official_hosts", "first_page", "observed_at"):
        if key not in data:
            raise DirectoryError(f"directory source missing {key}")
    return data


def discover(source: dict, fetch_page, jurisdiction="fed", max_pages=MAX_PAGES) -> DiscoveryResult:
    suffixes = tuple(source.get("official_suffixes", DEFAULT_OFFICIAL_SUFFIXES))
    source_hosts = set(source["official_hosts"])
    observed_at = source["observed_at"]
    components: dict[str, Component] = {}
    actions = []
    digest = hashlib.sha256()
    seen_pages = set()
    page_name = source["first_page"]
    pages = 0
    complete = True
    while page_name is not None:
        if page_name in seen_pages:
            actions.append({"page": page_name, "action": "manual_review", "reason": "pagination loop"})
            complete = False
            break
        if pages >= max_pages:
            actions.append({"page": page_name, "action": "manual_review", "reason": "page limit reached"})
            complete = False
            break
        seen_pages.add(page_name)
        page = fetch_page(page_name)
        pages += 1
        digest.update(canonical_json(page).encode("utf-8"))
        status = page.get("status")
        if status != 200:
            entry = {"page": page_name, "status": status}
            if status == 429 or (isinstance(status, int) and status >= 500):
                entry.update(action="retry_after", retry_after=page.get("retry_after", 60),
                             reason="rate limited" if status == 429 else "server error")
            else:
                entry.update(action="manual_review", reason="access denied" if status in (401, 403)
                             else "unexpected status")
            actions.append(entry)
            complete = False
            break
        page_problem = check_url(page.get("url", ""), suffixes)
        if page_problem or urlsplit(page["url"]).hostname not in source_hosts:
            actions.append({"page": page_name, "action": "manual_review",
                            "reason": f"page not served from the official source: {page_problem or 'host'}"})
            complete = False
            break
        for item in page.get("items", []):
            _merge_item(components, item, jurisdiction, suffixes, source_hosts, observed_at)
        page_name = page.get("next")
    for comp in components.values():
        if comp.status == "renamed":
            if not comp.renamed_to or comp.renamed_to not in components:
                actions.append({"component_id": comp.component_id, "action": "manual_review",
                                "reason": "renamed component successor not found"})
    return DiscoveryResult(source["source_id"], observed_at, digest.hexdigest(), complete, pages,
                           components, actions)


def _merge_item(components, item, jurisdiction, suffixes, source_hosts, observed_at):
    cid = component_id(jurisdiction, item["agency_abbr"], item["component_abbr"])
    status = item.get("status", "unknown")
    if status not in COMPONENT_STATUSES:
        status = "unknown"
    renamed_to = None
    if item.get("renamed_to"):
        renamed_to = component_id(jurisdiction, item["agency_abbr"], item["renamed_to"])
    comp = components.get(cid)
    if comp is None:
        comp = components[cid] = Component(cid, f"{jurisdiction}:{slug(item['agency_abbr'])}",
                                           item["agency_name"], item["component_name"], status,
                                           renamed_to=renamed_to)
    elif comp.name != item["component_name"]:
        comp.aliases.add(item["component_name"])
    comp.aliases.update(a for a in item.get("aliases", []) if a != comp.name)
    for raw in item.get("routes", []):
        route = build_route(cid, raw, suffixes, source_hosts, observed_at)
        comp.routes.setdefault(route.route_id, route)


def apply_staleness(route: dict, as_of: str, max_age_days=365) -> dict:
    """Mark a verified route stale when its verification is older than ``max_age_days``."""
    if route["state"] != "verified" or route["verified_at"] is None:
        return route
    verified = datetime.fromisoformat(route["verified_at"].replace("Z", "+00:00"))
    now = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if now - verified > timedelta(days=max_age_days):
        return dict(route, state="stale", reason=f"verification older than {max_age_days} days")
    return route


def persist(ledger, result: DiscoveryResult):
    """Store a discovery snapshot. Routes no longer listed are marked superseded."""
    now = ledger.clock()
    with ledger.transaction() as conn:
        if conn.execute("SELECT 1 FROM directory_snapshots WHERE snapshot_sha256=?",
                        (result.snapshot_sha256,)).fetchone():
            return False
        conn.execute("INSERT INTO directory_snapshots VALUES (?,?,?,?,?,?,?)",
                     (result.snapshot_sha256, result.source_id, result.observed_at, int(result.complete),
                      result.pages, canonical_json(result.actions), now))
        for comp in result.components.values():
            conn.execute(
                "INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(component_id) DO UPDATE SET "
                "agency_name=excluded.agency_name, name=excluded.name, status=excluded.status, "
                "aliases_json=excluded.aliases_json, last_seen_snapshot=excluded.last_seen_snapshot",
                (comp.component_id, comp.agency_id, comp.agency_name, comp.name, comp.status,
                 canonical_json(sorted(comp.aliases)), result.source_id, result.snapshot_sha256,
                 result.snapshot_sha256))
            listed = set(comp.routes)
            for route in comp.routes.values():
                conn.execute(
                    "INSERT INTO contact_routes VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(route_id) DO UPDATE "
                    "SET state=excluded.state, reason=excluded.reason, verified_at=excluded.verified_at, "
                    "redirects_json=excluded.redirects_json, last_seen_snapshot=excluded.last_seen_snapshot, "
                    "superseded_at=NULL",
                    (route.route_id, route.component_id, route.kind, route.destination, route.source_url,
                     route.state, route.reason, canonical_json(list(route.redirects)), route.verified_at,
                     result.snapshot_sha256, result.snapshot_sha256))
            if result.complete:
                # Only a complete listing can prove a route disappeared.
                for (route_id,) in conn.execute("SELECT route_id FROM contact_routes WHERE component_id=? "
                                                "AND superseded_at IS NULL", (comp.component_id,)).fetchall():
                    if route_id not in listed:
                        conn.execute("UPDATE contact_routes SET superseded_at=? WHERE route_id=?", (now, route_id))
            if comp.status == "renamed" and comp.renamed_to in result.components:
                ledger.add_component_alias(comp.component_id, comp.renamed_to,
                                           f"directory lists {comp.component_id} as renamed")
    return True


def component_record(ledger, cid):
    row = ledger.connection.execute("SELECT * FROM components WHERE component_id=?", (cid,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["aliases"] = json.loads(out.pop("aliases_json"))
    return out


def routes_for(ledger, cid, include_superseded=False):
    query = "SELECT * FROM contact_routes WHERE component_id=?"
    if not include_superseded:
        query += " AND superseded_at IS NULL"
    rows = ledger.connection.execute(query + " ORDER BY kind, route_id", (cid,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["redirects"] = json.loads(item.pop("redirects_json"))
        out.append(item)
    return out
