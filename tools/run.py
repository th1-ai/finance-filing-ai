#!/usr/bin/env python3
"""tools/run.py - Finance Filing AI's main loop: fetch -> extract -> code -> match -> file.

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 10
    python3 tools/run.py --once --provider mock

One pass: read unread mail from the invoice inbox, skip anything already
fully processed, run `tools/engine.py:process_invoice` on each new one. A
confident, clean invoice files itself automatically (in `mode: live` only -
see docs/how-it-works.md); an ambiguous or held one queues for a person.
Nothing here sends the daily summary - see `tools/digest.py` and
`workflows/15-daily-digest.md`.

Exit codes: 0 ok, 3 waiting on an `interactive` answer (see the message),
1 a real error.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402
from engine import process_invoice  # noqa: E402
from meter_feed import load_meter_rows  # noqa: E402
from po_ledger import load_po_ledger  # noqa: E402

log = get_logger("run")


def one_pass(settings, store: Store, *, limit: int, provider: str | None) -> tuple[int, dict]:
    """One pass over the inbox. With ``llm.provider: interactive``, a pass parks
    a prompt for EVERY item that needs one - not just the first - so a batch of
    new invoices takes as many passes as there are *stages* (extract, then
    categorize for any vendor not in ``known_vendors``), not one pass per
    invoice. See CLAUDE.md, "The interactive provider" and SIMULATION.md
    Finding 5: the old behaviour returned on the very first pend and could
    take ~15 round trips to clear a 9-invoice demo batch; this keeps going and
    only stops the pass (exit 3) once every message has been tried."""
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "skipped": 0}
    pending: list[LLMPendingInteractive] = []
    with Run("invoice-filing", settings, store) as run:
        email = get_email(settings)
        messages = email.fetch_unread(limit=limit)
        # A row still in `new` is NOT reported here - it must be retried, not
        # skipped. See docs/how-it-works.md, "Resumable stages".
        seen = store.already_processed("invoices", [m.id for m in messages])
        po_ledger = load_po_ledger(settings)
        meter_rows = load_meter_rows(settings)
        for msg in messages:
            if msg.id in seen:
                stats["skipped"] += 1
                continue
            try:
                item, did_work = process_invoice(settings, store, msg, po_ledger=po_ledger,
                                                 meter_rows=meter_rows, provider=provider)
            except LLMPendingInteractive as exc:
                # Park this one and keep going - the next message may need a
                # prompt too, and there is no reason to make the hotel come
                # back once per invoice to find that out.
                pending.append(exc)
                continue
            if not did_work:
                stats["skipped"] += 1
                continue
            stats["processed"] += 1
            if item.review_status == "auto_sent":
                stats["sent"] += 1
            elif item.review_status == "needs_human":
                stats["needs_human"] += 1
            else:
                # "dispatched" (would auto-file, --dry-run or shadow-blocked)
                # and "pending_review" both land here - queued, not sent.
                stats["drafted"] += 1
            log.info("processed", item_id=item.id, category=item.intent,
                     status=item.review_status)
        if not settings.dry_run and not pending:
            reaped = store.reap_stuck_sending()
            if reaped:
                log.warn("reaped stuck filings", count=len(reaped))
        run.stats = dict(stats)
    if pending:
        for exc in pending:
            print(str(exc))
        log.info("pending", count=len(pending))
        print(f"\n{len(pending)} prompt(s) parked in data/pending/ - answer each "
             f"*.prompt.md with a matching *.answer.json, then run this command again.")
        return 3, stats
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=50, help="max invoices per pass")
    parser.add_argument("--provider", default=None,
                        help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 3600)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 3600))
            while True:
                code, stats = one_pass(settings, store, limit=args.limit, provider=args.provider)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, limit=args.limit, provider=args.provider)
        print(summary_line(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
