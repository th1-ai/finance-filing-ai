# Workflow: working the review queue

Objective: turn a held invoice into a decision - approve, correct, or
reject - and file it; approve and send the daily digest. In `mode: shadow`,
approving records the decision only; nothing files or sends until you
switch to `mode: live` (`workflows/90-go-live.md`).

## Steps

1. **See what is waiting.**
   ```bash
   make review
   python3 tools/review.py list --kind invoice
   python3 tools/review.py list --status needs_human
   ```
   Each line shows the item id, status, kind, ledger category, and a short
   summary (vendor, amount, action for an invoice; subject for a digest).

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   Prints the extracted fields, the computed decision (`match`, `action`,
   `reason`, any `notes`), and the full event history. Read the reason to
   whoever owns the books in plain language before approving - "held
   because the invoice is nine percent over the purchase order, which is
   above our two percent tolerance" is useful; the raw JSON is not.

3. **Decide, for a held invoice.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py edit <id> --gl-code 6210 --gl-label "Housekeeping supplies"
   python3 tools/review.py edit <id> --action schedule --reason "confirmed with vendor, credit note issued"
   python3 tools/review.py reject <id> --reason "duplicate invoice"
   ```
   `edit` corrects the GL code, label, action or the recorded reason without
   re-running the model - use it when a person's judgement should simply
   override the computed decision (a vendor confirmed a price correction, a
   PO was found after the fact). `reject` discards the item; it does not
   file and is not retried.

4. **Decide, for a held digest.** Same commands - a digest almost always
   just needs `approve`; `edit --body-file <path>` replaces the whole body
   if you want to add something by hand first.

5. **File or send what was approved.**
   ```bash
   python3 tools/review.py send
   ```
   Claims everything `approved`/`edited` and finishes it: an invoice gets
   filed (`tools/engine.py:finalize_invoice` - the JSON record plus the
   finances-sheet row) and a digest gets emailed. In `mode: shadow` this is
   blocked outright, even for an item you just approved - shadow is a true
   kill switch (`docs/safety.md`).

6. **A failed file/send.** `send` marks the item `failed` with the error
   attached.
   ```bash
   python3 tools/review.py retry <id>
   ```
   re-queues it for another attempt once the cause is fixed.

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- Only `python3 tools/review.py send` writes `sending` / `sent` (and
  `auto_sent` is written only by the agent's own autonomous path in
  `tools/engine.py`, never by a human command).
- An invoice below the confidence threshold, or held for a PO/utility/
  threshold reason, never files itself - there is nothing to "approve your
  way past"; you correct the specific thing that made it ambiguous, or
  approve it as-is once you have satisfied yourself it is right.
- Confirm with whoever owns the books before switching this agent to
  `mode: live` the first few times, even though every write is already
  gated by approval where it needs to be.
