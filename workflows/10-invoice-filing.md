# Workflow: the invoice-filing loop

Objective: run one pass over the invoice inbox and see what Finance Filing
AI did with it. In `mode: live`, a confident invoice really files itself
here - nowhere else needs to happen for that. In `mode: shadow` (the
default), everything queues instead - see `workflows/80-review.md`.

## Inputs

- A configured `systems.email.adapter` (`mock` by default - see
  `workflows/00-setup.md` step 4 to connect a real inbox).
- `config/agent.yaml: known_vendors`, `gl_map`, `approved_vendors`,
  `matching`, `utility`, `confidence_threshold` - the defaults match the
  behaviour this agent was built from; your real numbers belong here.

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 10"      # just the first ten invoices
   make run ARGS="--dry-run"       # compute everything, write nothing at all
   ```
   Every new invoice goes through `tools/engine.py:process_invoice`: read
   the text (`extract`), pick a ledger category (a known-vendor lookup, or
   `categorize`), look up the GL code and its fixed confidence, then match
   it to a PO or run the no-PO rules. See `docs/how-it-works.md` for the
   full step-by-step and the mermaid diagram.

2. **If `llm.provider` is `interactive`,** the run stops with exit code 3
   once it has tried every invoice in the batch, parking a prompt in
   `data/pending/` for each one that needed a decision - not just the
   first. That is expected, not an error. Read every `*.prompt.md` file,
   work out the answer, write each as JSON to its matching `*.answer.json`
   (matching the schema exactly), and run the same command again. A single
   invoice can still need two passes of its own - one for `extract`, one
   for `categorize` - because the second call only happens once the first
   is answered; the agent never re-asks a question you already answered
   (`docs/how-it-works.md`, "Resumable stages"). A fresh batch is usually
   2-3 passes in total, not one pass per invoice.

3. **See what happened.**
   ```bash
   make review
   ```
   A confident invoice (GL confidence at or above `confidence_threshold`,
   matched or cleared) is `auto_sent` in live mode, or `pending_review` in
   shadow. An ambiguous or held one is `needs_human`, with the reason
   recorded - a low-confidence code, a PO variance, a utility bill over
   ceiling, a no-PO invoice over threshold or from an unapproved vendor.

4. **Work the queue.** `workflows/80-review.md` covers approve / edit /
   reject / send in full.

5. **Try a config change.** Add a vendor to `known_vendors`, run `make run`
   again on a fresh copy of one of its invoices - it now codes without
   calling `categorize` at all. Flip `rules.gl-auto: false` and every new
   invoice holds instead, with the reason recorded. Both change real
   behaviour, not a log line.

6. **Keep it running.**
   ```bash
   make watch                       # loop on the configured interval
   python3 tools/schedule.py --all   # cron/launchd/systemd snippets for every job
   ```
   `config/agent.yaml: schedule.invoice-filing` sets the cadence
   (`hourly` by default); `scheduler/` has ready-made cron, launchd and
   systemd files.

## Edge cases

- **No new invoices.** `make run` prints `0 items processed, 0 drafted, 0 sent`
  and exits 0.
- **A re-run sees the same invoice again.** `(source, external_id)` is
  unique on `items` - see `core.store.Store.upsert_item`. Nothing is
  redrafted, and `store.already_processed()` skips it before the engine
  even runs (unless it is still `new`, parked mid-pass - see
  `docs/how-it-works.md`).
- **A `po_ref` on the invoice that is not in the PO ledger.** Held, with the
  reason naming the missing reference - see `fixtures/inbound/invoice-09.json`
  for an example.
- **A vendor not in `known_vendors` and not obviously any category.** Coded
  `Sundry` at 55% confidence, always below the gate, always held - see
  `fixtures/inbound/invoice-07.json`.
