# Owner sending checklist

Sending is **disabled**. `Config` rejects `transport_enabled: true`, `dispatch` always exits non-zero, and no real transport adapter exists. Nothing in this checklist turns sending on. It lists what the owner must provide or approve before an engineering change may enable it, and the commands to run afterwards.

You can already do this without approving anything: prepare a review packet for any date range (it sends nothing).

```bash
python -m pip install .
foia-requests directory --source <official-directory-folder> --ledger ~/foia/ledger.sqlite
foia-requests prepare --ledger ~/foia/ledger.sqlite --coverage <coverage-export-folder> \
  --profile ~/foia/requester.json --last-days 7 --out ~/foia/packets/last-week
# then read ~/foia/packets/last-week/review-packet.md
```

Use `--start YYYY-MM-DD --end YYYY-MM-DD` for an exact range, `--calendar-year`/`--fiscal-year` for annual periods, and `--agency doj` (repeatable) or `--component fed:doj/oip` to narrow the agency set. `--last-days 7` means the seven complete days ending yesterday. End dates after today are rejected. Ranges ending in the last 30 days get a warning, because agencies may not have logged every request yet or may be slow to release recent entries. With the repository fixtures (`fixtures/directory`, `fixtures/aggregator_export`, `fixtures/profiles/synthetic-requester.json`) the packet is synthetic test output only.

## 1. Requester identity (owner provides)

Create a private profile file **outside the repository** (for example `~/foia/requester.json`, mode 0600). Pass it with `--profile`. It is printed in each request's signature block. Never commit it or paste it into chat, issues or PRs.

| Field | What to put |
|---|---|
| `profile_id` | Any stable label, e.g. `owner-2026` |
| `requester_principal` | Stable dedupe identity, e.g. `requester:owner`. Changing it creates new duplicate-check keys. |
| `name` | Name to sign requests with |
| `organization` | Organization, or `null` |
| `reply_email` / `reply_postal` | At least one reply route agencies can use |
| `verified` | `true` only after you have confirmed the details above |
| `synthetic` | `false` (the fixture is `true` and can never be authorized) |
| `fee_policy` | See section 5 |
| `delivery_format` | e.g. `CSV or spreadsheet preferred; PDF acceptable` |

## 2. Legal review of the template (owner approves)

The only template, `annual_foia_log_request@1` (`foia_requests/templates/`), has status `engineering_draft_pending_legal_review`. Every draft lists "approved template" as a missing input until it is approved. It works for any date range: it asks for the log of requests received from the start date through the end date. A reviewer should check:

- the 5 U.S.C. 552 citation and wording, against current official sources;
- the "existing log, not new records or annual report" language and the requested field list;
- the exclusion of other requesters' personal details;
- the fee paragraph, and any fee waiver wording;
- whether short or very recent ranges need extra wording (for example, asking for entries as currently maintained).

Approval is a PR that adds `annual_foia_log_request.v2.json` with `"status": "approved"`, the reviewer and review date in `authority_note`, and its hash in `LOCK.json`. Revision 1 is not edited in place.

## 3. Official routes and scope (owner approves)

- Load the directory from a verified official source. The repository fixtures use fake `.gov.example` addresses. Only `verified` email/portal routes can be authorized; anything else stays manual.
- Choose the agency set and period, then read the `prepare` packet. Check each destination, the dates and the "why included" lines.
- Large campaigns also need the FOIA Aggregator public-log coverage review (EXT-CORPUS), so agencies are not asked for logs that are already published.

## 4. Transport and its environment variables (owner chooses; engineering implements)

| Choice | What is needed |
|---|---|
| E-mail (SMTP) | A provider account for the reply address. Proposed variables: `FOIA_SMTP_HOST`, `FOIA_SMTP_USERNAME`, `FOIA_SMTP_PASSWORD`. |
| Agency portal API | Only where an agency publishes one. Proposed variable: `FOIA_PORTAL_API_TOKEN`. CAPTCHA or login-only portals stay manual. |
| Manual | Use the packet text and file each request yourself. Record receipts with the receipt tools. No credentials needed. |

`FOIA_TRANSPORT_PROVIDER` selects the transport. Today it accepts only `disabled` (default) and `fake` (tests only, with `FOIA_TRANSPORT_FAKE_ALLOWED=1`). The SMTP and portal variable names above are proposals: **no code reads them yet**. Keep credentials in the host's secret store, never in the repository.

## 5. Rate limits and fee ceiling (owner approves numbers)

- **Rate limits:** the worker defaults are 2 messages per destination host per hour and 10 per hour overall (`dispatch.RateLimits`). Each authorization also sets `max_messages` for its batch. Choose the numbers you approve. Lower is safer.
- **Fee ceiling:** `fee_policy.max_authorized_usd` in the profile is a whole-dollar cap. `0` (the default) means the request says you do not agree to pay fees and asks for an estimate first. Set `fee_waiver_basis` only if you have a truthful basis; otherwise leave it `null`. The software never pays fees.

## 6. Engineering change needed before any send (not done)

After items 1–5 are approved, someone must make a reviewed PR that:

1. implements the chosen adapter behind `FOIA_TRANSPORT_PROVIDER`, tested against the fake provider only;
2. allows `transport_enabled: true` only together with that adapter and its credentials;
3. adds CLI commands for authorization and dispatch (today these are library functions only: `authorization.authorize_batch`, `queue_authorized`, `dispatch.run_once`).

CI and tests must never contact an agency.

## 7. Command sequence after approval and that PR

Shown for planning. Steps 4–6 do not exist yet.

```bash
foia-requests directory --source <verified-official-directory> --ledger ~/foia/ledger.sqlite
foia-requests prepare --ledger ~/foia/ledger.sqlite --coverage <coverage-export> \
  --profile ~/foia/requester.json --start 2026-11-09 --end 2026-11-15 --out ~/foia/packets/2026-11-09
# 1. Read review-packet.md and choose up to 3 components for the first batch.
foia-requests draft --ledger ~/foia/ledger.sqlite \
  --coverage <coverage-export> --profile ~/foia/requester.json \
  --component fed:doj/oip --start 2026-11-09 --end 2026-11-15 --out ~/foia/batches
# 2. Read the batch packet (path printed as "packet"), then record the review (this does not authorize sending):
foia-requests review --ledger ~/foia/ledger.sqlite --packet <packet.json> --actor owner --note "reviewed"
# 3. Back up first (library: operations.verified_backup), then check:
foia-requests status --ledger ~/foia/ledger.sqlite
# 4. (future) authorize the exact batch: reference, expiry, max_messages, transport
# 5. (future) queue it, with FOIA_TRANSPORT_PROVIDER=<adapter> and its credentials in the environment
# 6. (future) run the dispatch worker, then record receipts and agency acknowledgments
```

To stop at any time, set the dispatch stop (`authorization.set_stop`). Never resend a request whose outcome is `submission_unknown`; reconcile it first.
