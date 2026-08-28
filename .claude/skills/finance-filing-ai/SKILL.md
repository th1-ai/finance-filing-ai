---
name: finance-filing-ai
description: Run Finance Filing AI ("The Bookkeeper") — Scans inboxes and Drive for invoices/receipts, reads each PDF, codes it to the right ledger category, matches it to its purchase order, files it to the right folder with a clean name, and logs it to the finances sheet, automatically.. Use when the user asks to run the agent, check what is waiting for review, approve or reject a draft, or asks how the agent is doing. Trigger phrases: "run The Bookkeeper", "/finance-filing-ai", "check the queue", "what is waiting for me", "approve that draft".
---

# Finance Filing AI

Runs Finance Filing AI and works its review queue. Everything happens from
the repo root; every command below exists and works.

## Before anything else

Read `README.md` if you have not this session, and
`workflows/10-invoice-filing.md` for the main loop. If the user has never
run this agent, start at `workflows/00-setup.md` instead and walk them
through it.

## The loop

**1. Check the agent is healthy.**

```bash
make doctor
```

Any `FAIL` line has a fix hint. Fix it before going further. `WARN` lines
are worth mentioning but do not stop the run. `pms adapter` and
`messaging adapter` are not relevant to this agent (`docs/integrations.md`).

**2. Run one pass over the invoice inbox.**

```bash
make run                        # one pass over new invoices
make run ARGS="--limit 5"       # just the first five
make run ARGS="--dry-run"       # compute everything, write nothing
```

If `llm.provider` is `interactive`, the run will stop with exit code 3 and
park prompts in `data/pending/`. That is expected - an invoice can pend
twice in one pass (`extract`, then `categorize`). Read each `*.prompt.md`,
write the answer as JSON to the matching `*.answer.json` following the
schema exactly, then run the same command again.

**3. Show what is waiting.**

```bash
make review
python3 tools/review.py show <id>
```

Summarise it for the user in plain language: vendor, amount, ledger
category, and why it was held (a low-confidence code, a PO variance, a
utility bill over ceiling, a no-PO invoice over threshold). Do not paste raw
JSON at them.

**4. Act on their decision.**

```bash
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --gl-code 6210 --gl-label "Housekeeping supplies"
python3 tools/review.py reject <id> --reason "<why>"
python3 tools/review.py send
```

`send` is what actually files an approved invoice (or emails an approved
digest) - blocked outright in `mode: shadow`, the same as everything else.

**5. Build and send the daily digest, if asked.**

```bash
python3 tools/digest.py
python3 tools/review.py list --kind digest
```

See `workflows/15-daily-digest.md`.

**6. Report.**

```bash
make report
```

## Rules

- **Never file or send in shadow mode**, and never work around a blocked
  write. The error message says what to do.
- **Going live is the property's decision.** Only raise it after
  `workflows/90-go-live.md` has been worked through - and note that once
  live, a confident invoice really does file itself with no further
  approval; say that plainly before switching.
- **Confirm before anything irreversible** - filing an invoice, sending the
  digest - even when it is approved, the first few times.
- **Never print or paste a credential.**
- If a run fails, read the whole error, fix the cause, re-run, and note what
  you learned in `workflows/99-troubleshooting.md`.
