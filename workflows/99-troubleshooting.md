# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`gl map`: gl_map or sundry is missing.** Copy
  `config/agent.example.yaml` to `config/agent.yaml`.
- **`email adapter` shows something other than `ok`.** Check
  `systems.email.adapter` in `config/hotel.yaml` and the matching
  `EMAIL_*`/`GOOGLE_*` variables in `.env` - see `docs/integrations.md`.
- **`pms adapter` or `messaging adapter` show something other than `ok`.**
  This agent does not use either - see `docs/integrations.md`. Safe to
  ignore.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` calls `load_settings(demo=True)`, which forces the mock
  provider, shadow mode and the mock adapter for every system - if you
  deleted or renamed `fixtures/inbound/*.json`, restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow
  errors on purpose, so a fixture problem shows up immediately.

## `python3 tools/run.py` exits 3

Not an error - `llm.provider: interactive` parked a prompt. Read
`data/pending/<id>.prompt.md`, write your answer as JSON to the matching
`.answer.json` file, and run the same command again. An invoice can pend
twice in one pass (`extract`, then `categorize`) - the second pend is
expected and the first answer is not lost; see `docs/how-it-works.md`,
"Resumable stages", if a second run seems to re-ask the first question
(it should not - that would be a real bug, not expected behaviour).

## A decision looks wrong

- **A vendor you know well keeps calling `categorize`.** Add it to
  `config/agent.yaml: known_vendors` with its category.
- **An invoice held for "not on the approved-vendor list" that should have
  cleared.** Add the vendor to `approved_vendors`. `known_vendors` and
  `approved_vendors` are separate lists on purpose - see
  `docs/how-it-works.md`, decision 9.
- **A PO variance held that you think should have cleared (or the
  reverse).** Check `matching.tolerance_pct` / `tolerance_eur` in
  `config/agent.yaml` - both must be breached to hold, either alone clears
  with a note.
- **A utility bill held with "No meter data available".** No
  `meter_feed.adapter` is connected, or the fixture/CSV has no rows for the
  billing window - see `docs/integrations.md#meter-feed`.
- **The wrong GL code.** Check `config/agent.yaml: gl_map` - the code and
  label are config, not something the model decides per invoice.

## `python3 tools/review.py send` says "blocked"

Expected while `mode: shadow` - shadow blocks every write, approved or not.
Handle it yourself for now, or go live (`workflows/90-go-live.md`).

## An item is stuck at `sending`

A process died between claiming an item and finishing it. `tools/run.py`
calls `core.store.Store.reap_stuck_sending()` on every real pass (skipped
during `--dry-run`), which moves anything stuck for more than 30 minutes to
`failed` so you see it in the queue instead of it vanishing. Use
`python3 tools/review.py retry <id>` once the cause is fixed.

## `python3 tools/*.py` says `ModuleNotFoundError: No module named 'core'`

You ran it with a Python that is not the repo's virtualenv, or from outside
the repo root. Use `make run` / `make doctor` / etc. (they call
`.venv/bin/python` for you), or run `.venv/bin/python tools/run.py` directly
from the repo root.

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one
item. If neither explains it, that is a real bug - describe exactly what
you ran and what you expected, and ask.
