# Send FOIA Log Requests — handoff status

Documentation baseline: **2026-10-11**. Source/PR verification: **2026-10-11T20:39:09.617Z**. This is a dated evidence ledger, not a live dispatch-status feed.

## Implementation status — 2026-11-15T18:58Z (supersedes the older "next unit" lines below)

Repository: `Justice-For-All-Movement/Send_FOIA_Log_Requests` (transferred from `neophilism`; registry identity and `scripts/validate_plan.py` were updated in #7 because the PR-registry contract compares the registry with `GITHUB_REPOSITORY`). Every PR below passed `offline-foundation`, `source-integrity` and `pr-registry-contract` on its exact head before merge. **No request, e-mail, portal submission or fee was sent or incurred. No live transport exists.**

| Unit | PR | Status |
|---|---|---|
| SFL-01 | #5 | Complete (earlier session). |
| SFL-05 | #7 | Complete: schema-migrated ledger, semantic dedupe key, state machine, overlap flags, aliases, amendments, historical import, verified backup/restore, multi-process dedupe. |
| SFL-04 | #8 | Complete: locked template revisions, deterministic rendering + manifest, missing-input markers, no automatic fee/waiver/private-field claims. The template stays `engineering_draft_pending_legal_review`, which keeps every real draft not live-ready until reviewed and approved. |
| SFL-02 | #9 | Complete against the acceptance fixtures (pagination, duplicates, renames/retired/unknown, route validation, 403/429 queue). Deferred: import of the Aggregator component census once FC-13 publishes it; a live directory adapter after the official endpoint/schema is verified. |
| SFL-03 | #10 | Complete over the export-fixture contract: explicit periods, coverage decisions, scope manifest with EXT-CORPUS gate (false for fixtures). |
| SFL-06 | #11 | Complete: capped dry run (DB-trigger cap), reproducible private review packet, zero-send guarantees, verified review recording; CLI `directory`/`draft`/`review`. |
| SFL-07 | #12 | Complete: roles, immutable authorization envelopes bound to content/route/destination/sender, transactional outbox, leases, stop flag, revocation on any change. |
| SFL-08 | #13 (contributing) | Worker, fake provider, hourly per-host/global caps, 429/401/403/bounce/timeout handling. Real adapters for verified official routes need EXT-DISPATCH. |
| SFL-09 | #14 | Complete: evidence-only reconciliation of unknown outcomes, idempotent provider events, agency acknowledgments with evidence hash. |
| SFL-10–14 | — | Not started (SFL-10 response intake is the next credential-free unit). |

**Aggregator dependency.** The consumer schema for FC-13 log coverage lives only in `foia_requests/aggregator_contract.py` (`fc13-consumer-draft-1`). When FOIA_Aggregator publishes FC-13, align that module and regenerate `fixtures/aggregator_export/`. `ext_corpus_satisfied` becomes true only for a live, commit-pinned export with an accepted FC-14 review. Large campaigns stay blocked until then.

**Tests.** `python scripts/check_foundation.py` → **93 product tests**, all passing; plus `python scripts/validate_plan.py`, `node scripts/validate-pr-registry.mjs`. CI also builds the pinned-setuptools wheel and runs the installed CLI fixture dry run, asserting zero outbound messages and fail-closed `dispatch`.

**Credentials/env vars for live use** (none exist or are committed):
- `FOIA_TRANSPORT_PROVIDER`: `disabled` (default) | `fake` (tests; also needs `FOIA_TRANSPORT_FAKE_ALLOWED=1`). Any other value raises `TransportNotImplemented`.
- A future reviewed adapter would need its own credentials, e.g. `FOIA_SMTP_HOST`/`FOIA_SMTP_USERNAME`/`FOIA_SMTP_PASSWORD` (e-mail) or `FOIA_PORTAL_API_TOKEN` (agency API). It would also need a reviewed change that lets `Config.transport_enabled` be true. That is currently rejected by design.
- Non-secret operational inputs: verified requester profile (identity, reply route, fee policy) supplied as a private profile file; explicit authorization reference/expiry/caps; read-only Aggregator export location.

**Blockers.** EXT-DISPATCH (authorized batch, verified routes, provider credentials, approved legal template), EXT-CORPUS (FC-13 export + FC-14 review), EXT-RUNTIME (only if hosted), EXT-RESPONSE (real agency responses).

Read [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) and [registry/prs.json](../registry/prs.json). The plan is independently actionable as a specification; no application, dispatch, fees or future schedule is authorized merely by its presence.

## Verified starting point

- Inspected main: [`33acf4a4c78102092e9653984bb50b09199e5385`](https://github.com/neophilism/Send_FOIA_Log_Requests/tree/33acf4a4c78102092e9653984bb50b09199e5385), before this documentation change. Baseline had only the title-only README and two registry files.
- GitHub returned zero PRs and zero Actions workflow runs. No application implementation, runtime tests, CI workflow, dependency manifest, migration, deployment config or `AGENTS.md` existed at that baseline.
- Current change supplies the comprehensive specification, handoff record, README, 14-unit structured plan, legacy roadmap redirect and a documentation validator. It does not supply the dispatcher or claim existing runtime acceptance.
- No Render/Neon/mail-provider/object-store/service credentials or deployed revision were inspected; hosting and transport are **unverified**, not certified absent. No government messages were sent, fees incurred or schedules activated for this task.

## Documentation and validation

SR-01–10 identify inherited/current requirements; SFL-01–14 are explicitly reconstructed completion units. Campaign years, requester identity, fee rules, verified recipients and provider are operational inputs, not recovered historical approvals. The plan specifies a reproducible offline preparation path and concrete acceptance gates without guessing those inputs.

Current executable documentation check, standard library only:

```bash
python scripts/validate_plan.py
```

No application setup/dispatcher/test command exists yet. `python -m unittest discover -s tests -v` becomes valid only after SFL-01 creates a nonempty suite. A zero-test result is not acceptance.

Publication validation also uses Engine Room's actual `parseStructuredPrRegistry` at commit `5491b7a75bbd7f4d82b4575283ad3929310ce50b` for the 14-unit denominator and empty completion mappings. Parser compatibility does not certify a live dashboard refresh. **PASS at 2026-10-11T20:42:30Z:** standard-library plan/registry validation and the actual Engine Room parser check. These are documentation results only.

## Remaining certification requirements

| Dimension | Current state | Required evidence |
|---|---|---|
| Standalone specification | Complete current reconstruction | Preserve source distinctions and executable scope as implementation changes. |
| Original detailed plan | Not recovered | Original source recovery or explicitly ratified new dated baseline. No invented historical year range, provider or fee policy. |
| Implementation | No application at inspected baseline | SFL-01–14, exact PR/commit mappings and nonempty acceptance suite. Documentation PR is not application progress. |
| Upstream readiness | Aggregator planning contract identified; real coverage unverified | Producer/export version + FC-14 coverage and relevant public-log dedupe evidence before large campaigns. |
| Dispatch authority | No concrete authorized batch for this task | Exact requester, official contacts, dates, text/attachments, delivery route, caps/fees, expiry and explicit dispatch authorization. |
| Real submission/fulfillment | None exercised by this task | Actual receipts/acknowledgments/responses and honest unresolved states; no automatic fulfillment claim from provider success. |
| Runtime and recovery | Not implemented/verified | Durable ledger/artifacts, secure provider access, actual release/host if selected, rollback/isolated restore and no duplicate send on resume. |
| Independent account | Instructions present; not independently executed | Fresh account sets up app, reproduces dry-run, verifies registry, tests restore and satisfies applicable external gates. |

Pinned upstream: [FOIA_Aggregator plan at `62fe82950963dcc0dde2003f094047d65bf79a9d`](https://github.com/neophilism/FOIA_Aggregator/blob/62fe82950963dcc0dde2003f094047d65bf79a9d/docs/DEVELOPMENT_PLAN.md), FC-01/03/08/13/14. This is not a working API or certified data-export version.

**Next ready unit: SFL-01**, within the plan's Aggregator-priority restriction; then directory/period/schema preparation. Refresh current main, open PRs and checks before starting. Run no invented command or real transport. The [26-repository audit](https://github.com/neophilism/Engine-Room/issues/71) links the final documentation merge and keeps remaining gates separate. Unconditional handoff certification remains **NOT CERTIFIED**.
