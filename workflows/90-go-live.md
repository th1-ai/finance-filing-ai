# Workflow: shadow to live

Objective: decide, together with whoever owns the books, whether Finance
Filing AI is ready to file confident invoices on its own instead of only
computing what it would do - and make the change safely if so.

This is the property's decision, never the agent's. Do not suggest it until
the checklist below is genuinely true, and when you do raise it, say
plainly what changes - and what does not.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it. `pms adapter` and `messaging adapter` are not
      relevant to this agent - ignore them (`docs/integrations.md`).
- [ ] `config/hotel.yaml` has the real property details. `config/agent.yaml`
      has your real `known_vendors`, `approved_vendors`, `gl_map` (your
      actual chart-of-accounts codes) and `matching`/`utility` numbers -
      not the shipped examples.
- [ ] At least a few real `make run` passes have gone through the review
      queue, not just the demo fixtures, and the held reasons look right
      for your invoices.
- [ ] You have connected a real `po_ledger.adapter` and `meter_feed.adapter`
      if you use purchase orders or want the utility cross-check to run for
      real - otherwise every PO-named or no-PO-utility invoice holds by
      default (`docs/integrations.md`).
- [ ] You are comfortable with `confidence_threshold` (0.90 by default) -
      raise it if you want more invoices to wait for you at first; lower it
      only once you trust the categorize model's judgement on your vendors.

## Making the change

1. Clear the shadow-era queue so nothing from testing goes out by surprise:
   ```bash
   python3 tools/review.py stale
   ```
2. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
3. `review.require_approval_for` still lists `send_email` by default -
   filing (`file_invoice`) and the sheet log (`sheets_write`) are
   deliberately not on that list, which is what lets a confident invoice
   file itself. Nothing about going live changes the confidence gate, the
   PO tolerance, or the no-PO rules - a held invoice is still always held.
4. Run `make doctor` again to confirm.
5. Run one real pass and watch a confident invoice file itself:
   ```bash
   make run ARGS="--limit 1"
   python3 tools/review.py list --kind invoice
   ```
   and, separately, approve and send one held invoice by hand to see the
   human-approved path work too.
6. Tell whoever owns the books exactly what just changed: a confident
   invoice now really files and logs itself, the next time
   `python3 tools/run.py` or the schedule runs. Everything else - anything
   below the confidence gate, a PO variance, a utility bill over ceiling, a
   no-PO invoice over threshold - still always waits for a person.

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every write on the next pass, mid-schedule, with no other change.
