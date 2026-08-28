#!/usr/bin/env python3
"""tools/review.py - work the review queue: list / show / approve / edit / reject / send.

    python3 tools/review.py list [--status needs_human] [--kind invoice]
    python3 tools/review.py show <id>
    python3 tools/review.py approve <id> [--note "..."]
    python3 tools/review.py edit <id> --gl-code 6210 --gl-label "Housekeeping supplies" [--note "..."]
    python3 tools/review.py reject <id> --reason "duplicate invoice"
    python3 tools/review.py retry <id>          # re-queue a failed file/send
    python3 tools/review.py send                # finish everything approved/edited
    python3 tools/review.py stale                # go-live step: see below

Only this tool writes `approved` / `edited` / `rejected` (core/review.py).
Only `send` writes `sending` / `sent`. Nothing here bypasses `mode: shadow` -
`mode: shadow` blocks every write, approved or not; only `mode: live` lets an
approved item actually file or send - see docs/safety.md.

`send` claims everything `approved`/`edited` and finishes it: an invoice
(`kind="invoice"`) gets filed and logged to the finances sheet
(`tools/engine.py:finalize_invoice`); a digest (`kind="digest"`) gets emailed
(`email.send()`). Both actions are guarded the same way.

`stale` is a `workflows/90-go-live.md` step: it moves every item still
waiting (held, approved or edited) to `stale` so nothing built up while you
were only testing in shadow goes out by surprise the moment you flip to
`live`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.log import get_logger  # noqa: E402
from core.review import (WriteBlocked, approve, edit, list_queue, reject,  # noqa: E402
                         retry, show, stale_backlog)
from core.store import Store, StoreError  # noqa: E402
from engine import finalize_invoice  # noqa: E402

log = get_logger("review")


def _print_item_line(item) -> None:
    payload = item.payload or {}
    draft = item.draft or {}
    if item.kind == "digest":
        label = payload.get("subject", "daily digest")
    else:
        label = f"{draft.get('vendor') or payload.get('from', '')} " \
               f"EUR {draft.get('amount_eur', '-')} {draft.get('action', '-')}"
    # `item.is_sample` is set by core (core/store.py) for anything read
    # through a mock adapter outside `make demo` - see docs/integrations.md
    # "Sample data is labelled".
    marker = "  [SAMPLE DATA]" if item.is_sample else ""
    print(f"  {item.id}  {item.review_status:<14} {item.kind:<8} {item.intent or '-':<12} "
         f"{label[:55]}{marker}")


def cmd_list(store, args) -> int:
    items = list_queue(store, status=args.status, kind=args.kind, limit=args.limit)
    if not items:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(items)} item(s) waiting:\n")
    for item in items:
        _print_item_line(item)
    print("\nRun `python3 tools/review.py show <id>` for the full record.")
    return 0


def cmd_show(store, args) -> int:
    try:
        detail = show(store, args.id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if (detail["item"].get("payload") or {}).get("_sample"):
        print("[SAMPLE DATA] this item was read through a mock adapter, not your "
             "property - see docs/integrations.md.\n")
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_approve(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    if not item.draft:
        print(f"error: {args.id} has no draft to approve yet", file=sys.stderr)
        return 1
    approve(store, args.id, note=args.note or "")
    log.info("approved", item_id=args.id, actor="human", note=args.note or None)
    print(f"approved {args.id} - now in the send queue")
    return 0


def cmd_edit(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    new_draft = dict(item.draft or {})
    if args.gl_code:
        new_draft["gl_code"] = args.gl_code
    if args.gl_label:
        new_draft["gl_label"] = args.gl_label
    if args.action:
        new_draft["action"] = args.action
    if args.reason:
        new_draft["reason"] = args.reason
    if args.body_file:
        new_draft["body"] = Path(args.body_file).read_text(encoding="utf-8")
    edit(store, args.id, new_draft, note=args.note or "")
    log.info("edited", item_id=args.id, actor="human", note=args.note or None,
             gl_code=args.gl_code, action=args.action)
    print(f"edited {args.id} - now in the send queue")
    return 0


def cmd_reject(store, args) -> int:
    reject(store, args.id, reason=args.reason or "")
    log.info("rejected", item_id=args.id, actor="human", reason=args.reason or None)
    print(f"rejected {args.id}")
    return 0


def cmd_retry(store, args) -> int:
    retry(store, args.id)
    log.info("retry_queued", item_id=args.id, actor="human")
    print(f"queued {args.id} for another attempt")
    return 0


def cmd_stale(store, args) -> int:
    moved = stale_backlog(store)
    log.info("stale_backlog", actor="human", moved=len(moved))
    print(f"marked {len(moved)} item(s) stale. Nothing from before go-live will file or send.")
    return 0


def cmd_send(store, settings, args) -> int:
    claimed = store.claim_for_send(limit=args.limit)
    if not claimed:
        print("Nothing approved or edited is waiting to send.")
        return 0
    filed, sent, failed = 0, 0, 0
    email = None
    for item in claimed:
        try:
            if item.kind == "invoice":
                result = finalize_invoice(settings, item)
                store.set_fields(item.id, sent_message_id=result.get("filed_path"))
                store.mark_sent(item.id, result.get("filed_path"))
                log.info("filed", item_id=item.id, actor="human", filed=result["filed_path"])
                print(f"filed {item.id} -> {result['filed_path']}")
                filed += 1
                continue
            if email is None:
                email = get_email(settings)
            draft = item.draft or {}
            payload = item.payload or {}
            result = email.send(payload.get("to") or [], draft.get("subject", ""),
                                draft.get("body", ""), item=item)
            store.mark_sent(item.id, result.get("message_id"))
            log.info("sent", item_id=item.id, actor="human",
                     message_id=result.get("message_id"))
            print(f"sent {item.id}")
            sent += 1
        except WriteBlocked as exc:
            # Not a failure: the mode blocked it. The approval stands for go-live.
            store.transition(item.id, "approved", "agent", {"blocked": str(exc)[:200]})
            log.warn("blocked_send", item_id=item.id, actor="human", reason=str(exc))
            print(f"blocked {item.id} (approval kept): {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001 - record and move on, never crash the batch
            store.mark_send_failed(item.id, str(exc))
            log.error("send_failed", item_id=item.id, actor="human", error=str(exc))
            print(f"failed {item.id}: {exc}")
            failed += 1
    print(f"\n{filed} filed, {sent} sent, {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="what is waiting for a human")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None, help="invoice | digest")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="full detail for one item")
    p_show.add_argument("id")

    p_approve = sub.add_parser("approve", help="approve the draft unchanged")
    p_approve.add_argument("id")
    p_approve.add_argument("--note", default="")

    p_edit = sub.add_parser("edit", help="correct the ledger code (or the digest body), then queue it")
    p_edit.add_argument("id")
    p_edit.add_argument("--gl-code", default=None)
    p_edit.add_argument("--gl-label", default=None)
    p_edit.add_argument("--action", default=None, choices=["schedule", "hold"])
    p_edit.add_argument("--reason", default=None, help="replace the stored reason text")
    p_edit.add_argument("--body-file", default=None, help="for a digest item")
    p_edit.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="discard the item")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default="")

    p_retry = sub.add_parser("retry", help="re-queue a failed file/send")
    p_retry.add_argument("id")

    p_send = sub.add_parser("send", help="finish everything approved or edited")
    p_send.add_argument("--limit", type=int, default=20)

    sub.add_parser("stale", help="go-live step: mark every waiting item stale")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "approve":
            return cmd_approve(store, args)
        if args.command == "edit":
            return cmd_edit(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "retry":
            return cmd_retry(store, args)
        if args.command == "send":
            return cmd_send(store, settings, args)
        if args.command == "stale":
            return cmd_stale(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        log.warn("blocked", actor="human", command=args.command, reason=str(exc))
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
