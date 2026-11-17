# Send FOIA Log Requests

Start with the [comprehensive development plan](docs/DEVELOPMENT_PLAN.md), then [handoff status](docs/HANDOFF_STATUS.md) and the [canonical structured PR registry](registry/prs.json).

The scope is official agency/component discovery, annual FOIA log request generation, a bounded dry run, controlled dispatch, deduplication, submission/fulfillment tracking and yearly continuation.

The specification is complete as a current engineering reconstruction. Original detailed campaign settings were not recovered. SFL-01–11 are implemented; SFL-12 and 13 partially (no hosting). The sending path (SMTP with STARTTLS, manual portal filing, `approve`/`queue`/`send`, emergency stop, receipts) exists but stays off until the owner supplies a verified profile, a legally approved template, transport credentials, `FOIA_SENDING_ENABLED=true` and a typed per-batch confirmation. The documentation and code do not authorize messages, fees or a future schedule. Large campaigns depend on the FOIA Aggregator corpus review described in the plan.

Validate the plan/registry and offline product suite using Python 3.11+ from this directory:

```bash
python scripts/validate_plan.py
python scripts/check_foundation.py

# CI also verifies the pinned setuptools wheel and installed CLI; see
# .github/workflows/offline-foundation.yml
```

The installed CLI is `foia-requests`. Preparation: `plan-logs` and `plan-followups` (below), `directory` (fixtures or the official FOIA.gov snapshot in `data/foia_gov_directory`), `draft` (bounded dry-run review packet), `prepare` (one readable Markdown review packet for a date range across all or filtered directory agencies; sends nothing), `review`, `status`. Owner-gated sending: `operator`, `approve template`, `approve batch`, `queue`, `send` (dry run unless every gate holds), `stop`/`resume`, `receipt manual|ack|reconcile`. `dispatch` is retired and fails closed; other families report not implemented. The library adds the request ledger, templates, coverage analysis against a versioned Aggregator export-fixture contract, exact-batch authorization/outbox and a fake-only dispatch worker. See [handoff status](docs/HANDOFF_STATUS.md). `registry/roadmap.json` redirects to `registry/prs.json`; maintain work IDs, dependencies and real PR mappings there rather than inventing totals from observed PR numbers.

Periods can be annual (`--calendar-year`, `--fiscal-year`) or any past date range (`--start/--end`, `--last-days N`). Preview the last week for every directory agency:

```bash
foia-requests prepare --ledger <private-ledger> --directory-source fixtures/directory \
  --coverage fixtures/aggregator_export --profile fixtures/profiles/synthetic-requester.json \
  --last-days 7 --out <private-folder>   # writes review-packet.md
```

## Periodic closed-request logs and released-records follow-ups (drafts only)

The FOIA Aggregator scrapes the logs agencies already publish. These two commands draft requests for the rest; neither sends anything, and both templates are `engineering_draft_pending_legal_review`.

- `plan-logs` drafts one `periodic_closed_request_log@1` request per directory component for the **next un-requested period of closed requests**. It plans every directory agency in one run (no cap unless `--max N`), in the civil-liberties priority order of [`foia_requests/data/priority.json`](foia_requests/data/priority.json) (tiers 1-3; unlisted agencies are tier 3). Cadence: tier 1 monthly, tiers 2-3 quarterly, unless `--cadence monthly|quarterly` sets it for the run or `--cadence-overrides <json>` (`[{agency_abbr, component_abbr|null, cadence}]`) sets it per agency (the override file wins over `--cadence`). A period is planned only after it has **fully ended**, with a warning if it ended under 30 days ago; the ledger keeps one request per component per period; components listed in `--published-logs <json>` (`[{agency_abbr, component_abbr|null, index_url, cadence}]`) are skipped (CIA, marked `always_request`, never is). Output: `plan.json` + `plan.md`, and each draft is stored in the ledger as a `draft` (`--no-record` to preview only).
- `plan-followups --gaps <dir>` reads an Aggregator `foia-release-gaps/1` export (`manifest.json` + `gaps.jsonl`, strictly validated; see [`fixtures/release_gaps`](fixtures/release_gaps)) and drafts `released_records_followup@1` letters asking for the records already released for closed requests the Aggregator lacks. Deduplicated by (agency, tracking number) against the ledger; `--agency`, `--max N` letters, and `--group` for one letter per component (at most 25 tracking numbers). Output: `followups.json` + `followups.md`.

```bash
foia-requests plan-logs --ledger <private-ledger> --directory-source fixtures/directory \
  --profile fixtures/profiles/synthetic-journalist.json --published-logs fixtures/published_logs.synthetic.json \
  --out <private-folder>
foia-requests plan-followups --ledger <private-ledger> --gaps fixtures/release_gaps \
  --profile fixtures/profiles/synthetic-journalist.json --group --out <private-folder>
```

**Cadence: run monthly or quarterly, never hourly.** Nothing schedules these commands. Running them more often cannot create new eligible periods (a period must end first), and agencies may treat many small requests from one requester as one aggregated request. FOIA reaches only records that exist when the agency searches, so requests are never prospective.

Both templates ask for a public-interest fee waiver (5 U.S.C. 552(a)(4)(A)(iii)). The fee category comes from the requester profile (`fee_category`: `news_media`, `educational` or `other`). `news_media` uses the outlet facts in the profile's `publication` (`name`, `url`, `audience`). The profile also needs a `dissemination` plan. Profiles state facts only. Missing facts appear as `[MISSING: ...]` and block live use. Drafts from these templates cannot pass `approve batch` today (it accepts only the annual template), so they cannot be sent without a further reviewed change.

Sending stays disabled until the owner completes the [owner sending checklist](docs/OWNER_SENDING_CHECKLIST.md). The FOIA.gov directory snapshot is refreshed by the manual "FOIA.gov directory snapshot" workflow (`scripts/fetch_foia_gov_directory.py`, `python -m foia_requests.foia_gov`).
