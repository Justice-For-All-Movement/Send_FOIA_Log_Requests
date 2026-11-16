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

The installed CLI is `foia-requests`. Preparation: `directory` (fixtures or the official FOIA.gov snapshot in `data/foia_gov_directory`), `draft` (bounded dry-run review packet), `prepare` (one readable Markdown review packet for a date range across all or filtered directory agencies; sends nothing), `review`, `status`. Owner-gated sending: `operator`, `approve template`, `approve batch`, `queue`, `send` (dry run unless every gate holds), `stop`/`resume`, `receipt manual|ack|reconcile`. `dispatch` is retired and fails closed; other families report not implemented. The library adds the request ledger, templates, coverage analysis against a versioned Aggregator export-fixture contract, exact-batch authorization/outbox and a fake-only dispatch worker. See [handoff status](docs/HANDOFF_STATUS.md). `registry/roadmap.json` redirects to `registry/prs.json`; maintain work IDs, dependencies and real PR mappings there rather than inventing totals from observed PR numbers.

Periods can be annual (`--calendar-year`, `--fiscal-year`) or any past date range (`--start/--end`, `--last-days N`). Preview the last week for every directory agency:

```bash
foia-requests prepare --ledger <private-ledger> --directory-source fixtures/directory \
  --coverage fixtures/aggregator_export --profile fixtures/profiles/synthetic-requester.json \
  --last-days 7 --out <private-folder>   # writes review-packet.md
```

Sending stays disabled until the owner completes the [owner sending checklist](docs/OWNER_SENDING_CHECKLIST.md). The FOIA.gov directory snapshot is refreshed by the manual "FOIA.gov directory snapshot" workflow (`scripts/fetch_foia_gov_directory.py`, `python -m foia_requests.foia_gov`).
