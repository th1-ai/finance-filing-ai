# Guardrails and safety

This agent touches your books and, once live, files things on its own for
the invoices it is confident about. Everything below is built in, not
optional, and this page explains what it does and what is left for you to
decide.

## What Finance Filing AI specifically will not do

- **Never posts an uncoded invoice.** If `rules.gl-auto` is off, or the
  ledger category lands below `confidence_threshold` (90% by default), the
  invoice is held for a person - no GL code, no filing, no sheet row -
  `tools/engine.py:process_invoice`.
- **Never pays without a purchase order above the no-PO threshold** (€1,000
  by default). A non-utility invoice at or above it, with no `po_ref`, is
  always held for a retrospective PO - `tools/engine.py:no_po_branch`.
- **Never clears a small no-PO invoice from a vendor that is not on the
  approved-vendor list**, even under the threshold - see
  `config/agent.yaml: approved_vendors` and docs/how-it-works.md decision 9.
- **Never clears a utility bill it cannot reconcile to consumption.** No
  meter data, or more than `utility.tolerance_pct` (15% by default) above
  the contracted ceiling, and it is held - `tools/engine.py:utility_check`.
- **Never lets a percentage breach alone stop a payment.** A PO variance
  must breach both the percentage AND the euro tolerance to hold; breaching
  only one clears with a note ("Logged for the vendor review, not held") -
  `tools/engine.py:three_way_match`.
- **Never schedules a payment or releases money.** This agent stops at
  "clear to file" versus "held" - see docs/how-it-works.md, decision 11.
  A sibling agent in this family (Procure-to-Pay AI) owns the payment run.
- **Never invents an extracted figure.** `prompts/extract.md` tells the
  model to work only from the text in front of it; a genuinely unreadable
  field is a job for the confidence gate and a person, not a guess.

### Rule-off honesty

Every disabled rule states the exposure it creates, not just a log line.
`rules.gl-auto: false` forces every invoice held, with the reason "with
auto GL coding off there is no ledger code to post against."
`rules.utility-anomaly: false` clears a no-PO utility bill without checking
the meter, with the note "a runaway bill would clear unseen." Both are real
behaviour, not cosmetic wording - see `tools/engine.py:process_invoice`.

### The one autonomous path in this repo, and its brake

Unlike every other template in this family, a confident invoice here really
can go from inbox to filed with nobody touching it - that is the roster's
own promise ("files the bulk of invoices fully automatically, daily"). The
brake is the 90% confidence gate plus the PO/no-PO rules above: everything
that reaches the autonomous path already passed all of them. And it still
only runs in `mode: live` - see the next section.

## The two modes

| Mode | What happens |
|---|---|
| `shadow` (default) | The agent reads, extracts, codes and decides. It **never** files an invoice and **never** writes a sheet row - not even the confident ones. A confident invoice queues as `pending_review` instead of filing itself. |
| `live` | A confident invoice really files itself (`review_status: auto_sent`). A held invoice, once you approve or edit it, really files on the next `python3 tools/review.py send`. Everything else still waits. |

`mode` lives in `config/hotel.yaml`. It is a global kill switch: flipping it
back to `shadow` stops every write immediately, mid-schedule, with no other
change. `config/agent.yaml` can be stricter than `hotel.yaml`, never looser.
**Shadow blocks every write, approved or not** - approving a held invoice in
shadow only records your decision; nothing files until you switch to
`mode: live` (`workflows/90-go-live.md` runs `python3 tools/review.py stale`
first, so nothing built up during shadow goes out by surprise).

Two more brakes:

- `make run ARGS="--dry-run"` computes and prints the exact decision for
  every new invoice and writes nothing at all: no database row, no cache,
  no filed JSON, no sheet row. See docs/how-it-works.md, design decision 14,
  for exactly what that does and does not touch.
- `review.require_approval_for` in `config/hotel.yaml` lists the actions
  that need a human even in live mode. The defaults are `send_email`,
  `send_message`, `pms_write`, `payment`, `publish`. Filing an invoice
  (`file_invoice`) and logging a sheet row (`sheets_write`) are
  deliberately **not** on that list - that is what makes the autonomous
  path possible once you trust it. They are still blocked outright by
  `mode: shadow`, the same as everything else.

Every write in the codebase goes through one function,
`core/review.py:assert_write_allowed`. There is no second path.

## The review queue

Nothing files or sends without either clearing the gates above on its own,
or a person looking at it.

```bash
make review                        # what is waiting
python3 tools/review.py show <id>   # the full extraction, decision and reason
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --gl-code 6210 --gl-label "Housekeeping supplies"
python3 tools/review.py reject <id> --reason "duplicate invoice"
python3 tools/review.py send        # files/sends everything approved or edited
```

A held invoice moves `new → needs_human`. A confident one moves
`new → dispatched → auto_sent` (live) or `new → dispatched → pending_review`
(shadow, or `--dry-run`, or the write was blocked for any other reason).
Only `tools/review.py` writes `approved` / `edited` / `rejected`; only
`python3 tools/review.py send` writes `sending` / `sent`. A crash between
"about to file" and "filed" is picked up on the next pass
(`store.reap_stuck_sending`) and shown to you as failed, not silently retried.

## What the agent will not do

- File or log anything while `mode: shadow`.
- File an item a human has not approved, when it needed one.
- Take a payment, issue a refund, or move money at all. There is no payment
  adapter wired into this repo.
- Guess a ledger code below the confidence threshold, or reconcile a bill it
  has no data to check.

## Data handling

**What leaves your machine.** With `llm.provider: anthropic` or
`claude-code`, the invoice text (from `extract`) and the extracted summary
(from `categorize`) go to Anthropic. With `mock` or `interactive`, nothing
leaves the machine at all.

**What is stored, and where.** Everything lives in `data/` inside this
folder: `agent.db` (SQLite), `logs/*.jsonl`, `exports/` (filed invoice
records and the finances CSV, if you use the `csv` sheets adapter).
`data/` is gitignored. There is no cloud service behind this repo and no
telemetry.

**Card numbers are redacted on the way in.** Every inbound email passes
through `core/redact.py` before it is stored, logged or put into a prompt.
A payment card number is replaced with `[CARD REDACTED ****1234]`; labelled
CVC and expiry values in the same message go with it. IBANs are masked the
same way - which matters here, since a supplier invoice sometimes carries
its own bank details. Detection requires a real card prefix and a valid
Luhn checksum, so invoice and PO reference numbers survive. Nothing in
config turns this off.

**Retention.** `privacy.retention_days` (default 365) is how long processed
invoices stay in the database. Deleting `data/agent.db` deletes everything
the agent knows; it does not delete anything already filed to
`data/exports/` or written to a real Google Sheet.

## GDPR, in practice

Invoices rarely carry EU personal data beyond a contact name on a supplier
letterhead, but the general shape still applies:

- **You are the controller.** This software runs on your machine, under your
  control, on your data. TH1 does not receive it.
- **Your model provider is a processor.** If you use `anthropic` or
  `claude-code`, Anthropic processes what is in the prompt on your behalf.
  Check their data processing terms and record them in your processing
  register.
- **Purpose and minimisation.** The agent sees the invoice text and the
  property facts it needs - nothing from `knowledge/` beyond that.
- **Retention.** Set `privacy.retention_days` to what your own bookkeeping
  and tax retention rules require, not the default.

This is a practical summary, not legal advice.

## Telling people they are talking to AI

There is no guest-facing text in this agent at all - it never emails a
supplier, never replies to anyone outside the business. The one message it
sends, the daily digest, goes to your own manager or controller
(`contacts.escalation_email`), not a third party, so the EU AI Act Article
50 disclosure that guest-facing repos in this family carry does not apply
here in the same way. If you forward the digest outside the business (to an
external bookkeeper, for instance), say plainly in your own covering note
that the summary was produced by an AI agent under your supervision.

## Subscription or API: an honest note

`extract` and `categorize` run on every new invoice - this is where most of
the spend is, not the optional controller's note. `interactive` or
`claude-code` (your own Claude Code subscription) cost nothing extra and
are fine at the volume most independent properties see. `anthropic` (a
metered API key) is the right choice once you are running this on a
schedule, unattended, at real volume - `python3 tools/report.py` shows the
running total either way.

Read Anthropic's usage policy before pointing a busy invoice inbox at your
personal subscription around the clock; a handful of scheduled hourly
passes is normal, an unattended high-volume mailbox is not.

## If something goes wrong

1. `mode: shadow` in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env`.
   Every write stops on the next pass.
2. Remove the schedule (`crontab -e`, `launchctl unload`, or
   `systemctl disable --now <slug>.timer`).
3. `make doctor` to see what the agent thinks its state is.
4. `data/logs/*.jsonl` has every decision, with the run id, in order.
5. A wrongly filed invoice: the record in `data/exports/filed/<category>/`
   and the finances-sheet row are both plain files/rows - delete or correct
   them by hand, then fix the source of the mistake (a `gl_map` confidence
   set too high, a vendor added to `known_vendors` or `approved_vendors` in
   error) so it does not repeat.
