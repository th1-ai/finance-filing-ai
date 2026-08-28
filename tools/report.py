#!/usr/bin/env python3
"""tools/report.py - what the agent did, and what it cost.

    make report
    python3 tools/report.py
    python3 tools/report.py --export

The roster's promise is "files the bulk of invoices fully automatically,
daily; turns month-end scramble into a non-event" and "-90% invoice-filing
labor". This prints the numbers that let you check that promise against
what actually happened: how much filed itself versus how much needed you,
the edit rate on corrections you made, and the spend.

`--export` also writes the same row to `systems.sheets.adapter` (csv by
default: `data/exports/finance_filing_report.csv`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_sheets  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402


def _amount(row) -> float:
    try:
        draft = json.loads(row["draft_json"]) if row["draft_json"] else {}
        return float(draft.get("amount_eur") or 0)
    except (TypeError, ValueError):
        return 0.0


def gather(store: Store) -> dict:
    rows = store.db.execute("SELECT * FROM items WHERE kind='invoice'").fetchall()
    total = len(rows)
    filed = [r for r in rows if r["review_status"] == "auto_sent"]
    sent_by_human = [r for r in rows if r["review_status"] == "sent"]
    held = [r for r in rows if r["review_status"] in ("needs_human", "pending_review")]
    auto_filed_value = sum(_amount(r) for r in filed)
    held_value = sum(_amount(r) for r in held)

    edited_ids = {r["item_id"] for r in store.db.execute(
        "SELECT DISTINCT item_id FROM events WHERE action='status:edited'").fetchall()
        if r["item_id"]}
    reviewed = len(held) + len(sent_by_human)

    ages = []
    for row in rows:
        if row["sent_at"] is None:
            continue
        try:
            created = datetime.fromisoformat(row["created_at"])
            sent = datetime.fromisoformat(row["sent_at"])
            ages.append((sent - created).total_seconds() / 3600)
        except (TypeError, ValueError):
            continue

    cost_usd = 0.0
    for row in store.db.execute(
        "SELECT detail_json FROM events WHERE action='llm_call'").fetchall():
        try:
            cost_usd += float((json.loads(row["detail_json"]) or {}).get("cost_usd") or 0.0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    return {
        "total_invoices": total, "auto_filed": len(filed), "auto_filed_value_eur":
            round(auto_filed_value, 2), "held_or_queued": len(held), "held_value_eur":
            round(held_value, 2), "filed_by_human": len(sent_by_human),
        "auto_filed_pct": round(100 * len(filed) / total, 1) if total else 0.0,
        "edit_rate_pct": round(100 * len(edited_ids) / reviewed, 1) if reviewed else 0.0,
        "avg_hours_to_finish": round(sum(ages) / len(ages), 1) if ages else None,
        "llm_cost_usd": round(cost_usd, 4), "by_status": store.counts(),
    }


def print_report(stats: dict, mode: str) -> None:
    print("Finance Filing AI - report\n")
    print(f"  Invoices seen so far:    {stats['total_invoices']}")
    print(f"  Filed fully automatically: {stats['auto_filed']} "
         f"(EUR {stats['auto_filed_value_eur']}), {stats['auto_filed_pct']}% of everything seen")
    print(f"  Needed a human (held or queued): {stats['held_or_queued']} "
         f"(EUR {stats['held_value_eur']})")
    print(f"  Filed after human review: {stats['filed_by_human']}")
    print(f"  Edit rate:               {stats['edit_rate_pct']}% of reviewed invoices "
         f"were corrected before filing")
    avg = stats["avg_hours_to_finish"]
    print(f"  Average time to finish:  {avg} hour(s)" if avg is not None
         else "  Average time to finish:  nothing finished yet")
    print(f"  LLM spend so far:        ${stats['llm_cost_usd']} (extract + categorize, plus "
         f"the optional controller's note)")
    print("\n  By status: " + ", ".join(f"{k}={v}" for k, v in sorted(stats["by_status"].items())))
    print(f"\n  Mode: {mode}. In shadow, 'filed fully automatically' above is really 'would "
         f"have filed once live' - see docs/how-it-works.md.")


def export_csv(settings, stats: dict) -> str:
    sheets = get_sheets(settings)
    header = ["generated_at", "total_invoices", "auto_filed", "auto_filed_value_eur",
             "auto_filed_pct", "held_or_queued", "held_value_eur", "filed_by_human",
             "edit_rate_pct", "avg_hours_to_finish", "llm_cost_usd"]
    row = [datetime.now(timezone.utc).isoformat(timespec="seconds"), stats["total_invoices"],
          stats["auto_filed"], stats["auto_filed_value_eur"], stats["auto_filed_pct"],
          stats["held_or_queued"], stats["held_value_eur"], stats["filed_by_human"],
          stats["edit_rate_pct"], stats["avg_hours_to_finish"] or "", stats["llm_cost_usd"]]
    sheets.append("finance_filing_report", [header, row])
    return "finance_filing_report"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", action="store_true",
                        help="also write the numbers via systems.sheets.adapter")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        stats = gather(store)
        print_report(stats, settings.mode)
        if args.export:
            sheet = export_csv(settings, stats)
            print(f"\nExported to: {sheet} ({settings.systems.sheets.adapter} adapter)")
        return 0
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        print("--export writes via systems.sheets.adapter, which mode: shadow blocks like "
             "every other write. The numbers above are still accurate.", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
