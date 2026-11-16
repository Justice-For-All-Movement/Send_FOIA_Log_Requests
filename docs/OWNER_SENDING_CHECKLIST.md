# Owner sending checklist

Sending is **disabled** until you do the steps below. The complete sending path is built and tested (SMTP e-mail, manual portal filing, `approve`, `queue`, `send`, emergency stop, receipts), but `foia-requests send` only transmits when **all five** of these hold at once:

1. a requester profile that is `verified: true`, `synthetic: false`, and stored **outside** the repository;
2. an approved template revision (`status: approved`) whose hash is pinned in `foia_requests/templates/LOCK.json`, and a batch authorized with that exact revision;
3. a configured transport: `FOIA_TRANSPORT_PROVIDER=smtp` with every `FOIA_SMTP_*` value set (STARTTLS is enforced), or `FOIA_TRANSPORT_PROVIDER=manual`;
4. `FOIA_SENDING_ENABLED=true` in the environment of that command;
5. the per-batch phrase typed exactly: `--confirm "SEND <n> <auth-id>"`.

Without `--confirm`, `send` is a dry run: it lists the messages and every unmet condition, and sends nothing. None of these conditions is met in the repository, CI or tests; tests use only a local fake SMTP server on 127.0.0.1 and fake `.gov.example` addresses.

Everything below is done once on your own computer, from a checkout of this repository. Commands assume `~/foia` as your private folder.

```bash
git clone https://github.com/Justice-For-All-Movement/Send_FOIA_Log_Requests.git && cd Send_FOIA_Log_Requests
python3 -m pip install -e .
mkdir -p -m 700 ~/foia
```

## 1. Requester identity (owner provides)

```bash
cp fixtures/profiles/synthetic-requester.json ~/foia/requester.json && chmod 600 ~/foia/requester.json
```

Edit `~/foia/requester.json`. It is printed in each request's signature block. Never commit it or paste it into chat, issues or PRs (`.gitignore` already excludes `requester*.json`).

| Field | What to put |
|---|---|
| `profile_id` | Any stable label, e.g. `owner-2026` |
| `requester_principal` | Stable dedupe identity, e.g. `requester:owner`. Changing it creates new duplicate-check keys. |
| `name` | Name to sign requests with |
| `organization` | Organization, or `null` |
| `reply_email` / `reply_postal` | At least one reply route. `reply_email` becomes the `Reply-To` of every e-mail. |
| `verified` | `true` once the details above are correct |
| `synthetic` | `false` |
| `fee_policy` | `{"max_authorized_usd": 0, "fee_waiver_basis": null}` unless you choose otherwise (section 5) |
| `delivery_format` | e.g. `CSV or spreadsheet preferred; PDF acceptable` |

## 2. Legal review of the template (owner approves)

The draft template `annual_foia_log_request@1` (`foia_requests/templates/annual_foia_log_request.v1.json`) has status `engineering_draft_pending_legal_review`. Have it reviewed for: the 5 U.S.C. 552 citation and wording; the "existing log, not new records or annual report" language and the field list; the exclusion of other requesters' personal details; the fee paragraph; and wording for short or very recent ranges.

Then record the approval. It writes revision 2 with `status: approved`, the reviewer **role** (not a personal name) and date, and pins its hash in `LOCK.json`:

```bash
foia-requests approve template          # prints the exact confirmation phrase; writes nothing
foia-requests approve template --reviewer-role "owner's counsel" --reviewed-on 2026-11-20 \
  --confirm "I CONFIRM LEGAL REVIEW OF annual_foia_log_request@1 0ff85013bb25"
```

If the reviewer changed the wording, put the new `subject`/`body`/`requested_fields` in a JSON file and add `--source edited.json`. You may commit the two changed files (`annual_foia_log_request.v2.json`, `LOCK.json`) in a PR, or keep them local; both work. To keep them outside the checkout instead, copy `foia_requests/templates` to a private folder, set `FOIA_TEMPLATE_DIR` to it, and approve there.

## 3. Official routes and scope (owner approves)

Get the official FOIA.gov agency directory (public contact data published by FOIA.gov) into the repository once:

1. Sign up for a free key at https://api.data.gov/signup/ (the shared `DEMO_KEY` runs out after about 10 pages).
2. Add it as a repository secret: GitHub, Settings, Secrets and variables, Actions, New repository secret, name `FOIA_GOV_API_KEY`.
3. Actions, "FOIA.gov directory snapshot", Run workflow. It fetches, converts, validates and pushes the branch `automation/foia-gov-directory`.
4. Open a PR from that branch (https://github.com/Justice-For-All-Movement/Send_FOIA_Log_Requests/compare/main...automation/foia-gov-directory) and merge it when its checks pass. Then `git pull`.

Load it into your ledger:

```bash
foia-requests directory --source data/foia_gov_directory --ledger ~/foia/ledger.sqlite
foia-requests operator --ledger ~/foia/ledger.sqlite --id owner --role all
```

Only routes on official `.gov`/`.mil` hosts listed by FOIA.gov are `verified`; others are never sent to. Preview any period across all or some agencies (sends nothing):

```bash
foia-requests prepare --ledger ~/foia/ledger.sqlite --coverage fixtures/empty_coverage_export \
  --profile ~/foia/requester.json --last-days 7 --agency doj --out ~/foia/packets/preview
```

`fixtures/empty_coverage_export` means "no public-log coverage data yet". Large campaigns should wait for the FOIA Aggregator public-log coverage review (EXT-CORPUS), so agencies are not asked for logs they already publish.

## 4. Transport and its environment variables (owner chooses and supplies)

| Choice | Set in your shell (never in the repository) |
|---|---|
| E-mail (SMTP) | `FOIA_TRANSPORT_PROVIDER=smtp`, `FOIA_SMTP_HOST`, `FOIA_SMTP_PORT` (default 587; must offer STARTTLS; 465 is refused), `FOIA_SMTP_USERNAME`, `FOIA_SMTP_PASSWORD` (an app password), `FOIA_SMTP_FROM` (the address agencies see; usually your `reply_email`). Optional: `FOIA_SMTP_TIMEOUT`. |
| Manual (portal-only agencies) | `FOIA_TRANSPORT_PROVIDER=manual`. `send` writes ready-to-paste text, a PDF and a checklist; you file each request yourself. No credentials. |

```bash
export FOIA_TRANSPORT_PROVIDER=smtp FOIA_SMTP_HOST=smtp.your-provider.example FOIA_SMTP_PORT=587 \
  FOIA_SMTP_USERNAME=you@your-domain.example FOIA_SMTP_FROM=you@your-domain.example
read -rs FOIA_SMTP_PASSWORD && export FOIA_SMTP_PASSWORD
```

A batch is authorized for one transport. `draft` picks a verified e-mail route when one exists, otherwise a verified portal; authorize e-mail drafts with `--transport smtp` and portal drafts with `--transport manual` (`approve batch` tells you if a request needs the other one).

## 5. Rate limits and fee ceiling (owner approves numbers)

- **Rate limits:** `send` defaults to 2 messages per destination host per hour, 10 per hour overall, and 30 seconds between messages (`--per-host-per-hour`, `--global-per-hour`, `--min-interval-seconds`; capped at 20 and 60). `--max N` sends at most N in one run.
- **Fee ceiling:** `fee_policy.max_authorized_usd` is a whole-dollar cap; `0` says you do not agree to pay fees and asks for an estimate first. Set `fee_waiver_basis` only if you have a truthful basis. The software never pays fees.

## 6. What the software guarantees (no action needed)

- Each request is sent at most once: one live outbox intent per request; submitted, failed and unknown attempts are never re-sent automatically. Running `send` again on a finished batch reports `nothing_pending`.
- If a connection drops after the message was handed to the server, the request becomes `submission_unknown` and the batch is blocked until you reconcile it: `foia-requests receipt reconcile --ledger ~/foia/ledger.sqlite --attempt <id> --operator owner --outcome submitted --source provider_sent_folder --reference "<Message-ID>"` (check your mail provider's Sent folder first).
- An authentication failure sets the emergency stop. Content, route, profile or template changes after approval revoke the batch.
- Every accepted e-mail stores a receipt in the ledger (SMTP reply, Message-ID, SHA-256 of the exact message). Provider acceptance is not an agency acknowledgment.

## 7. Command sequence after approval

```bash
L=~/foia/ledger.sqlite
# a. Draft and review up to 3 requests (repeat for more batches).
foia-requests draft --ledger $L --coverage fixtures/empty_coverage_export --profile ~/foia/requester.json \
  --component fed:doj/oip --start 2026-01-01 --end 2026-10-31 --out ~/foia/batches
foia-requests review --ledger $L --packet <packet.json printed above> --actor owner --note "reviewed"
# b. Authorize exactly that batch (sends nothing), then queue it.
foia-requests approve batch --ledger $L --operator owner --profile ~/foia/requester.json \
  --transport smtp --reference OWNER-2026-11-20-1 --packet <packet.json>
foia-requests queue --ledger $L --operator owner --auth-id <auth-id printed above>
# c. Dry run: read the list and the blockers. Then send with the printed phrase.
foia-requests send --ledger $L --auth-id <auth-id> --profile ~/foia/requester.json
FOIA_SENDING_ENABLED=true foia-requests send --ledger $L --auth-id <auth-id> \
  --profile ~/foia/requester.json --confirm "SEND <n> <auth-id>"
# Manual (portal) batches: same steps with --transport manual, FOIA_TRANSPORT_PROVIDER=manual and
#   --manual-out ~/foia/manual; then follow CHECKLIST-<auth-id>.md and record each filing:
foia-requests receipt manual --ledger $L --attempt <attempt-id> --operator owner \
  --confirmation <portal-number> --evidence <saved-confirmation.pdf>
# d. When an agency acknowledges a request:
foia-requests receipt ack --ledger $L --request <request-id> --operator owner \
  --tracking-number <agency-number> --evidence <saved-email.eml>
foia-requests status --ledger $L
```

**Emergency stop**, at any time (takes effect before the next message): `foia-requests stop --ledger ~/foia/ledger.sqlite --operator owner --reason "..."`. Undo with `foia-requests resume` (same arguments). Unsetting `FOIA_SENDING_ENABLED` also halts a running batch before its next message.

Back up the ledger before and after each batch (`operations.verified_backup`), and keep `~/foia` private.
