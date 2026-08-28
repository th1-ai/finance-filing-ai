# Workflow: first-run setup

Objective: get Finance Filing AI from a fresh clone to coding real invoices,
in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never
   overwrites your own copies). `make doctor` will show a `FAIL` on "hotel
   identity" right after setup - that is expected, it means the property
   name is still the shipped placeholder. It also prints `pms adapter` and
   `messaging adapter` lines: this agent does not use either, ignore them
   (see `docs/integrations.md`).

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see 9 sample invoices from `fixtures/inbound/` sorted into
   "would auto-file" and "needs a human", and the line
   `DEMO OK — 9 items processed, 4 would auto-file once live, 5 need a human (shadow)`.
   If you do not see that, stop and read `workflows/99-troubleshooting.md`.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address,
   contact, currency). Then edit `config/agent.yaml`:
   - `known_vendors` - your recurring suppliers, mapped to a ledger
     category. A vendor listed here never calls the categorize model.
   - `approved_vendors` - who may clear a small no-PO invoice.
   - `gl_map` - your real GL codes and labels, one row per category.
   - `matching` / `utility` - your real PO tolerance and utility tariffs.
   - `confidence_threshold` - leave at 0.90 unless you have a specific
     reason to move it (`docs/safety.md`).

4. **Connect a real invoice inbox.** `systems.email.adapter` in
   `config/hotel.yaml` starts as `mock`, which only ever sees the bundled
   fixtures. `imap` reads a real mailbox - see `docs/integrations.md`. Also
   connect `po_ledger.adapter` and `meter_feed.adapter` in
   `config/agent.yaml` (`csv`, pointing at exports in `data/imports/`) if
   you use purchase orders or want the utility cross-check to run for real.

5. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real, "hotel identity" turns green. Move on to
   `workflows/10-invoice-filing.md` to run the loop on your own invoices.
