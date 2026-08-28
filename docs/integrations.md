# Connecting your systems

Every connector in this repo is one of three things, and the table says which.
We will not tell you an integration exists when it does not.

| Badge | Means |
|---|---|
| **built** | Written against the real API and tested against it. |
| **universal** | Works with any system through a common protocol: IMAP/SMTP, CSV, a webhook, a fixture reader. |
| **stub** | Interface only. Calling it raises a clear error with a recipe for adding it. |

Check what is actually working on your machine at any time:

```bash
make doctor
```

This agent uses four systems: **email** (the invoice inbox, and the daily
digest send), **sheets** (the finances log and the report export), plus two
small readers of its own - the **PO ledger** and the **meter feed** - that
are not core adapters yet (see "Core requests" below). It does not use a PMS
or WhatsApp/chat messaging.

## Email - `systems.email.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/*.json`. What `make demo` uses. |
| `imap` | universal | mailbox + app password | Any provider. **Start here for a real property.** |
| `gmail` | built | Google OAuth desktop client | Adds labels/threads. |

Point this at whatever mailbox receives supplier invoices and receipts - a
dedicated `invoices@` or `accounts@` alias works best, so nothing this agent
should not see lands in it. In `.env`:

```
EMAIL_ADDRESS=invoices@example.com
EMAIL_PASSWORD=            # an APP password, never your login password
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
```

**"Reads each PDF" is not yet real OCR.** `extract` (the model call in
`tools/engine.py`) reads whatever text the email adapter hands it -
`EmailMessage.body_text`, or `extra.raw_text` if your fixture/import sets
it. A genuinely scanned PDF attachment needs a text-extraction step first
(pdf-to-text or an OCR library) that this repo does not ship - see
"Implement your own" below for the recipe. Plenty of suppliers already send
invoices as plain-text or HTML email with a PDF copy attached for the
record; those work today with zero extra code.

**The daily digest also sends through this adapter.** `python3 tools/digest.py`
queues a `kind="digest"` item; `python3 tools/review.py send` calls
`email.send()` once you approve it, the same guarded `send_email` action
every repo in this family uses.

## Sheets - `systems.sheets.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `csv` | universal | nothing | Writes `data/exports/finances.csv` and `data/exports/finance_filing_report.csv`. |
| `google` | built | service account JSON | A live shared spreadsheet - "the finances sheet" the roster promises. |

`tools/engine.py:log_to_finances_sheet` appends one row per filed invoice
(`filed_at, item_id, vendor, invoice_no, category, gl_code, gl_label,
amount_eur, po_ref, match, action, filed_name`) - this is the "logs it to
the finances sheet" half of the promise. `python3 tools/report.py --export`
writes the benefit numbers to a second sheet.

For `google`: enable the Sheets API, create a service account and a JSON
key, save it as `service_account.json`, share your spreadsheet with the
service account's email as an Editor, and set
`systems.sheets.spreadsheet_id` to the id from the sheet's URL.

## PO ledger - `config/agent.yaml: po_ledger.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/purchase-orders.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/purchase_orders.csv`. **Start here for a real property.** |

Columns: `po_ref, vendor, amount_eur, description, received`. This is what
`tools/engine.py:three_way_match` checks an invoice's `po_ref` against - the
roster's "matches it to its purchase order". Export it from whatever
procurement or accounting system holds your open purchase orders; a missing
or unmatched `po_ref` is a normal, handled outcome (held for a person), not
an error.

## Meter feed - `config/agent.yaml: meter_feed.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/meter-readings.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/meter_readings.csv`. |

Columns: `day_offset, kwh, water_m3, occupied_rooms`. This is what
`tools/engine.py:utility_check` sums over the trailing
`utility.window_days` to price a no-PO utility bill against the contracted
tariff. Export it from your BMS or your utility portal's usage history. No
feed configured means every no-PO utility invoice is held with "No meter
data available for the billing window" - safe, but it means a person looks
at every one until you connect a real feed.

## Not used by this agent

**PMS.** `make doctor` still prints a `pms adapter` line (every repo in this
family shares the same generic health check) - this agent does not read
reservations, rates or occupancy from it. The per-room-night context line
in a utility hold's notes comes from the meter feed's own
`occupied_rooms` column, not a PMS call.

**Messaging (WhatsApp/chat).** No escalation alert is wired up. Add one by
following `docs/integrations.md#implement-your-own` in a sibling repo (any
repo that uses `systems.messaging.adapter` shows the pattern) if you want a
message the moment something is held, rather than waiting for the digest.

## Implement your own

<a id="implement-your-own"></a>

**A real document-extraction step.** Open `claude` in this folder and paste:

> Read `tools/engine.py:invoice_to_dict` and `prompts/extract.md`. I want a
> real PDF-to-text step before `extract` runs: <pdfplumber / an OCR library
> / a vision-capable model call - say which>. Attachments arrive at
> `<describe how you'll get the PDF bytes to this machine>`. Write it so
> `EmailMessage.extra["raw_text"]` ends up holding the extracted text before
> `tools/engine.py:process_invoice` reads it, and add a test with one real
> (redacted) invoice PDF as a fixture.

**A real PO ledger or meter feed integration** (rather than CSV). Copy
`tools/po_ledger.py` or `tools/meter_feed.py` as the shape - they are short.
Implement `load_po_ledger`/`load_meter_rows` against your system's API,
keep the same return shape (`PoLedger` / a list of row dicts), and switch
`config/agent.yaml: po_ledger.adapter` / `meter_feed.adapter` to a name you
register.

**A general adapter** (mailbox, sheet, or anything in `core/adapters/base.py`).
The five-step recipe every repo in this family uses:

1. Copy the closest existing adapter (`core/adapters/email_imap.py`,
   `core/adapters/sheets_csv.py`).
2. Implement `ping()` and `capabilities()` first - `make doctor` reads both.
3. Implement the reads, mapping onto the dataclasses in `core/adapters/base.py`.
4. Implement the writes, each with `@guarded_write("<action>")` - not optional,
   or your adapter can write while the agent is in shadow mode.
5. Register it in `core/adapters/__init__.py`'s `REGISTRY`, set the adapter
   name in `config/hotel.yaml`, and run `make doctor`.

### Rules that matter

- **`ping()` never raises.** Return `HealthCheck(ok=False, ...)` with a hint.
- **Every write is decorated**, or the write guard cannot see it.
- **Never log a credential.** `core/log.py` masks anything whose key looks
  like a secret, but do not rely on it.
- **Redact on ingestion.** Any inbound text goes through `core.redact.redact()`
  before it is stored, logged or put into a prompt - the email adapters do
  this for you already.
- **Write a test.** Copy `tests/test_finance_filing_loop.py`'s pattern:
  build `Settings` from a tmp copy of the shipped `.example.yaml` files,
  feed your reader a fixture, check the dict/dataclass that comes out.

### Core requests

`tools/po_ledger.py` and `tools/meter_feed.py` live in `tools/` rather than
`core/adapters/` because `core/` is vendored byte-for-byte into all 28 repos
in this family from a single factory source. A "Core request" to add a
`procurement`-backed PO ledger (fixture + CSV, the way `pms_csv.py` already
works for reservations) to `core/adapters/__init__.py`'s registry is noted
in this repo's build report, for whenever a second agent in the family
needs the same shape.
