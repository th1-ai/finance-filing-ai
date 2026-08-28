#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled invoices, zero credentials.

    make demo
    python3 tools/demo.py

Loads `load_settings(demo=True)`, which forces `mock` provider, `shadow` mode
and the `mock` adapter for every system regardless of config/hotel.yaml, so
this always works on a fresh clone with a blank .env (ARCHITECTURE.md
section 1, "works in 5 minutes with zero credentials") and never reads a
real mailbox. It runs against its own database (data/demo/demo.db) so
running it twice always shows the same nine fixtures, and never touches
data/agent.db (that is `make run`'s file).

Because `mode` is forced to shadow, nothing here ever actually files an
invoice or writes a sheet row - even the confident, would-auto-file ones
stop at `pending_review` instead of `auto_sent`. That is expected: shadow is
the global kill switch. See docs/how-it-works.md.

Prints one line every check reads for the pass/fail signal:

    DEMO OK — 9 items processed, 5 auto-filed (shadow-blocked, queued), 4 need a human
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.store import Store  # noqa: E402
from engine import process_invoice  # noqa: E402
from meter_feed import load_meter_rows  # noqa: E402
from po_ledger import load_po_ledger  # noqa: E402


def main() -> int:
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()  # every `make demo` is a clean, repeatable run
    store = Store(settings, path=demo_db)

    email = get_email(settings)
    messages = email.fetch_unread(limit=50)
    if not messages:
        print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
        return 1

    po_ledger = load_po_ledger(settings)
    meter_rows = load_meter_rows(settings)

    stats = {"processed": 0, "auto_would_file": 0, "needs_human": 0}
    print(f"Finance Filing AI demo - {len(messages)} sample invoice(s) from fixtures/inbound/\n")
    for msg in messages:
        item, _ = process_invoice(settings, store, msg, po_ledger=po_ledger,
                                  meter_rows=meter_rows, provider="mock")
        draft = item.draft or {}
        stats["processed"] += 1
        if item.review_status == "needs_human":
            stats["needs_human"] += 1
            tag = "NEEDS_HUMAN"
        else:
            stats["auto_would_file"] += 1
            tag = "would-auto-file (shadow-blocked)"
        print(f"  {msg.id}: {draft.get('vendor', '?')} EUR {draft.get('amount_eur', '?')} "
             f"-> category={item.intent} confidence={item.confidence:.2f} "
             f"gl={draft.get('gl_code') or '-'} match={draft.get('match') or '-'} [{tag}]")

    print(f"\n{stats['needs_human']} of {stats['processed']} need a person to look first - "
         f"below the {float(settings.agent_get('confidence_threshold', 0.90)):.0%} confidence "
         f"gate, a price variance, or a hold with no matching PO (see docs/safety.md).")
    print("Nothing was filed or logged: mode is shadow, and the write guard blocks it "
         "regardless of confidence - see docs/how-it-works.md.")
    print("Next: `make review` to see what is waiting, or read workflows/10-invoice-filing.md.\n")

    print(f"DEMO OK — {stats['processed']} items processed, "
         f"{stats['auto_would_file']} would auto-file once live, "
         f"{stats['needs_human']} need a human ({settings.mode})")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
