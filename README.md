# Send FOIA Log Requests

Start with the [comprehensive development plan](docs/DEVELOPMENT_PLAN.md), then [handoff status](docs/HANDOFF_STATUS.md) and the [canonical structured PR registry](registry/prs.json).

The scope is official agency/component discovery, annual FOIA log request generation, a bounded dry run, controlled dispatch, deduplication, submission/fulfillment tracking and yearly continuation.

The specification is complete as a current engineering reconstruction. Original detailed campaign settings were not recovered. SFL-01–07 and SFL-09–11 are implemented offline; SFL-08, 12 and 13 partially (fake transport only, no hosting); live dispatch is disabled. The documentation and code do not authorize messages, fees or a future schedule. Large campaigns depend on the FOIA Aggregator corpus review and exact dispatch authorization described in the plan.

Validate the plan/registry and offline product suite using Python 3.11+ from this directory:

```bash
python scripts/validate_plan.py
python scripts/check_foundation.py

# CI also verifies the pinned setuptools wheel and installed CLI; see
# .github/workflows/offline-foundation.yml
```

The installed CLI is `foia-requests`. `directory`, `draft` (bounded dry-run review packet), `prepare` (one readable Markdown review packet for a date range across all or filtered directory agencies; sends nothing) and `review` are implemented over local fixtures; `dispatch` fails closed; other families report not implemented. The library adds the request ledger, templates, coverage analysis against a versioned Aggregator export-fixture contract, exact-batch authorization/outbox and a fake-only dispatch worker. See [handoff status](docs/HANDOFF_STATUS.md). `registry/roadmap.json` redirects to `registry/prs.json`; maintain work IDs, dependencies and real PR mappings there rather than inventing totals from observed PR numbers.

Periods can be annual (`--calendar-year`, `--fiscal-year`) or any past date range (`--start/--end`, `--last-days N`). Preview the last week for every directory agency:

```bash
foia-requests prepare --ledger <private-ledger> --directory-source fixtures/directory \
  --coverage fixtures/aggregator_export --profile fixtures/profiles/synthetic-requester.json \
  --last-days 7 --out <private-folder>   # writes review-packet.md
```

Sending stays disabled. Before it can be enabled, the owner needs to provide and approve the items in the [owner sending checklist](docs/OWNER_SENDING_CHECKLIST.md).
