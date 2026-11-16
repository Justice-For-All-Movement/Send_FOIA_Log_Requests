# Send FOIA Log Requests — handoff status

Documentation baseline: **2026-10-11**. Source/PR verification: **2026-10-11T20:39:09.617Z**. This is a dated evidence ledger, not a live dispatch-status feed.

## Update — 2026-11-16: owner-gated sending path (SFL-08/09 completion)

- Merged as #25 (head `5a525d9`, all check runs `success`). SFL-08 maps to #25; #25 contributes to SFL-02 (FOIA.gov import) and SFL-09 (SMTP/manual receipts).
- FOIA.gov snapshot: the first workflow run fetched 10 real pages (500 components) and then hit the shared `DEMO_KEY` quota (HTTP 429, Retry-After about 4 hours). The fetcher now fails fast on a long Retry-After, always uploads raw pages, and the workflow pushes a validated snapshot to `automation/foia-gov-directory`. A free api.data.gov key in the `FOIA_GOV_API_KEY` secret is needed for a complete fetch (checklist section 3).

- New: `smtp_transport.py` (STARTTLS required and verified before credentials; definite vs unknown outcome classification; receipt with SMTP reply, Message-ID and SHA-256 of the exact wire message), `manual_filing.py` + `pdf.py` (ready-to-paste text, PDF and checklist for portal-only agencies; `receipt manual` records the filing), `sending.py` (the five-condition gate and the per-batch send loop with rate limits, minimum interval, emergency-stop and env-flag re-checks before every message), `foia_gov.py` + `scripts/fetch_foia_gov_directory.py` + the manual "FOIA.gov directory snapshot" workflow (official FOIA.gov agency components into `data/foia_gov_directory`).
- CLI: `operator`, `approve template`, `approve batch`, `queue`, `send` (dry run by default), `stop`, `resume`, `receipt manual|ack|reconcile`. `dispatch` stays retired and fails closed.
- Gate (all required): verified non-synthetic profile outside the checkout; approved template with approval record and LOCK.json hash, matching the batch; `FOIA_TRANSPORT_PROVIDER` smtp (all `FOIA_SMTP_*` set) or manual, matching the batch; `FOIA_SENDING_ENABLED=true`; typed `SEND <n> <auth-id>`. The repository meets none of them. `Config` files still cannot enable transport.
- Tests use a local fake SMTP server on 127.0.0.1 with a throwaway certificate and the `.gov.example` fixtures only. No agency was contacted; nothing was sent.
- Owner's remaining steps: [OWNER_SENDING_CHECKLIST.md](OWNER_SENDING_CHECKLIST.md).

## Update — 2026-11-16T02:05Z: custom periods and the `prepare` packet

- #22 (SFL-03, contributing): any past date range via `--start/--end` or `--last-days N` (N complete days ending yesterday), in addition to `--calendar-year`/`--fiscal-year`. End dates after today, inverted ranges and pre-FOIA starts are rejected. Ranges ending within 30 days get a warning that agencies may not have logged or released recent entries yet. `draft` takes `--component` plus a period option. `--select` and annual behaviour are unchanged.
- #23 (SFL-06, contributing): `foia-requests prepare` writes one readable `review-packet.md` (plus a hashed `prepare-manifest.json`) for a period across all directory components, or those picked with `--agency`/`--component`. Each entry gives the route, why it is included or excluded, the missing inputs and the exact draft text. It is a preview: no ledger request writes, no sender, zero outbound messages. CI runs it from the installed wheel.
- [OWNER_SENDING_CHECKLIST.md](OWNER_SENDING_CHECKLIST.md) lists what the owner must provide or approve before sending can be enabled: identity, legal review, routes, transport and its variables, rate limits, fee ceiling, the engineering change still needed, and the command sequence. `scripts/validate_plan.py` now checks the checklist is present, complete and linked.
- Tests: **131** product tests pass. Sending is still disabled. Nothing was sent.

## Implementation status — 2026-11-15T19:20Z (supersedes the older "next unit" lines below)

Repository: `Justice-For-All-Movement/Send_FOIA_Log_Requests` (transferred from `neophilism`; the registry identity was updated in #7). Every PR below passed `offline-foundation`, `source-integrity` and `pr-registry-contract` on its exact head before merge. **No request, e-mail, portal submission or fee was sent or incurred. No live transport exists.**

| Unit | PR(s) | Status |
|---|---|---|
| SFL-01 | #5 | Complete. |
| SFL-02 | #9, #16 | Complete: fixture directory discovery, plus import of the FC-13 catalog agency/office census (`directory --catalog`). Census-only components are `unknown` with no routes and a `verify_contact_routes` action, because FC-13/1 carries no contacts. A live official-directory adapter still needs endpoint/schema verification. |
| SFL-03 | #10 (#16, #22 contributing) | Complete over the coverage contract; #22 adds custom date ranges. FC-13/1 has no log-coverage export, so that dataset is now the consumer proposal `sfl-log-coverage-proposal-1`; EXT-CORPUS stays closed. |
| SFL-04 | #8 | Complete; the template stays `engineering_draft_pending_legal_review`. |
| SFL-05 | #7 | Complete. |
| SFL-06 | #11 (#23 contributing) | Complete; #23 adds the full-scope `prepare` preview packet. |
| SFL-07 | #12 | Complete. |
| SFL-08 | #13 (contributing) | Worker, fake provider and rate limits; real adapters need EXT-DISPATCH. |
| SFL-09 | #14 | Complete. |
| SFL-10 | #17 | Complete: manual/upload-first response intake, attachment quarantine, period×field fulfillment accounting, follow-up tasks (no automatic appeals, deadlines or payments). |
| SFL-11 | #18 | Complete: restart-safe yearly continuation (drafts only, no inherited authorization); scheduler disabled by default. |
| SFL-12 | #19 (contributing) | `sfl-interchange/1` bundle export/import with privacy guard and idempotent acknowledgments. Needs a FOIA_Aggregator import endpoint/commit to pin. |
| SFL-13 | #20 (contributing) | Full-digest verified backups, isolated restore (no resend), health/status reporting. Hosting/scheduler/off-host backup need EXT-RUNTIME. |
| SFL-14 | — | Blocked: needs EXT-CORPUS, EXT-DISPATCH and (if hosted) EXT-RUNTIME. Status: "implementation/dry-run only; live pilot blocked". |

**Aggregator contract.** `foia_requests/aggregator_contract.py` pins FOIA_Aggregator `ca822fad6bc089a48ede6d89d94cdad47967b11f` (PR #72, `foia-catalog-export/1`). The schema is vendored at `foia_requests/schemas/catalog-record.v1.schema.json` with its SHA-256 pinned. `fixtures/fc13_producer_snapshot/` was generated by the Aggregator's own `write_snapshot`.

**Tests.** `python scripts/check_foundation.py` → **119 product tests**, all passing; plus `python scripts/validate_plan.py`, `node scripts/validate-pr-registry.mjs`. CI also builds the wheel and runs the installed fixture dry run, asserting zero sends and fail-closed `dispatch`.

**Credentials/env vars for live use** (none exist or are committed):
- `FOIA_TRANSPORT_PROVIDER`: `disabled` (default) | `fake` (tests, plus `FOIA_TRANSPORT_FAKE_ALLOWED=1`). Any other value raises `TransportNotImplemented`.
- A future reviewed adapter would need its own credentials, e.g. `FOIA_SMTP_HOST`/`FOIA_SMTP_USERNAME`/`FOIA_SMTP_PASSWORD` or `FOIA_PORTAL_API_TOKEN`. It would also need a reviewed change allowing `Config.transport_enabled`.
- Operational inputs: verified requester profile, approved legal template, authorization reference/expiry/caps, FC-13 snapshot location (read-only), and a host for EXT-RUNTIME.

**Blockers.** EXT-DISPATCH, EXT-CORPUS (an upstream log-coverage export plus an accepted FC-14 review), EXT-RUNTIME, EXT-RESPONSE.

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
