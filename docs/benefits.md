# The business case

**Why.** Receipt admin is the chore everyone hates and does late. This
keeps the books current and audit-ready with near-zero effort.

**Output.** Files the bulk of invoices fully automatically, daily; turns
month-end scramble into a non-event.

**ROI.** -90% Invoice-filing labor (labor).

(Quoted verbatim from the roster - see `README.md` for the full promise.)

## The problem this solves

Invoice filing is the job that gets pushed to the end of the day, then the
end of the week, then into a shoebox for month-end. It is not hard work,
just relentless: a PDF a day, sometimes several, each one needing a glance,
a folder, a filename, and a line in a spreadsheet. Skip it for two weeks and
the books stop being current; skip it for a month and the close takes a day
instead of an hour. This agent does the part that is genuinely repetitive -
reading the document, picking the ledger code, matching it to a PO if there
is one, filing it under a clean name, logging it - and leaves the part that
actually needs a person: the ~10% that is genuinely ambiguous.

## What to measure

`python3 tools/report.py` reads straight from `core.store` and shows:

- **Auto-filed rate**: the share of every invoice seen that filed itself
  with no human touch - the number that lets you check "files the bulk of
  invoices fully automatically" against what actually happened on your
  invoices, not the demo's.
- **Held value**: euros waiting for a human, alongside the count - a small
  held count with a large held value (one big variance) reads very
  differently from a large held count of small ones (your confidence
  threshold or vendor table need attention).
- **Edit rate**: how often a held invoice's ledger code or action was
  corrected before filing - a falling edit rate over time is the signal
  that `known_vendors` and `gl_map` match your actual chart of accounts.
- **Average time to finish**: from first seen to filed or sent - the
  practical measure of "turns month-end scramble into a non-event."
- **Spend**: `extract` and `categorize` run on every new invoice (see
  `docs/safety.md`, "Subscription or API"); this is the real number to
  watch, not the optional controller's note.

`python3 tools/report.py --export` writes the same numbers to
`systems.sheets.adapter` so you can hand a controller a file instead of a
terminal.

## Honest caveats

- **"−90% invoice-filing labor" is the source material's own estimate, not
  a guarantee for your property.** It assumes most of your invoices come
  from a small, recurring set of vendors (which is when `known_vendors`
  does the most work) and that your PO discipline is reasonably consistent.
  A property with mostly one-off, unpredictable suppliers will see more
  held invoices and a smaller share auto-filed - `python3 tools/report.py`
  tells you your own number.
- **"Reads each PDF" needs a real text-extraction step for scanned
  invoices.** As shipped, `extract` reads whatever text your mailbox
  already carries (plain-text/HTML email, or `extra.raw_text` if you wire
  one in) - see `docs/integrations.md`. A property that receives mostly
  scanned image PDFs needs that step built first.
- **"Matches it to its purchase order" stops at "clear to file" versus
  "held".** No payment is scheduled or released by this agent - see
  `docs/how-it-works.md`, decision 11.
- **Approved-vendor and known-vendor lists are only as good as you keep
  them.** A new recurring supplier not yet added to `known_vendors` calls
  the `categorize` model every time and, for a small no-PO invoice, is held
  until you add them to `approved_vendors` - both are one-line additions to
  `config/agent.yaml`.
