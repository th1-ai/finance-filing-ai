#!/usr/bin/env python3
"""tools/digest.py - build (and queue) the daily "what I filed" summary email.

    python3 tools/digest.py
    python3 tools/digest.py --date 2026-08-27

Gathers everything this agent touched since the last digest (or, on the
first run, everything it has ever seen), builds a plain-text summary
deterministically, and queues it as a `kind="digest"` item - the same review
FSM as an invoice, so it needs the same `approve` + `send` before it goes
out (`review.require_approval_for: send_email` by default). One digest per
calendar day: re-running the same day updates the queued draft instead of
creating a second one.

The optional cosmetic controller's note (`tools/narrate.py`,
`narrate.enabled` in config/agent.yaml, off by default) is appended if it is
switched on and the LLM provider answers in time; a failure there never
blocks the digest itself.

Exit codes: 0 ok (queued, or nothing to report), 1 a real error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

log = get_logger("digest")


def gather_since(store: Store, since_iso: str | None) -> dict:
    sql = "SELECT * FROM items WHERE kind='invoice'"
    params: list[str] = []
    if since_iso:
        sql += " AND updated_at >= ?"
        params.append(since_iso)
    rows = store.db.execute(sql, params).fetchall()
    filed = [r for r in rows if r["review_status"] == "auto_sent"]
    held = [r for r in rows if r["review_status"] == "needs_human"]
    queued = [r for r in rows if r["review_status"] == "pending_review"]

    import json as _json

    def draft_of(row) -> dict:
        try:
            return _json.loads(row["draft_json"]) if row["draft_json"] else {}
        except (TypeError, ValueError):
            return {}

    filed_total = sum(float(draft_of(r).get("amount_eur") or 0) for r in filed)
    held_total = sum(float(draft_of(r).get("amount_eur") or 0) for r in held)
    held_detail = [
        {"vendor": draft_of(r).get("vendor", "?"), "amount": draft_of(r).get("amount_eur"),
         "reason": draft_of(r).get("reason", "")}
        for r in held[:12]
    ]
    return {"filed": len(filed), "filed_total_eur": round(filed_total, 2), "held": len(held),
            "held_total_eur": round(held_total, 2), "queued": len(queued),
            "held_detail": held_detail}


def build_body(hotel_name: str, stats: dict, narrative: str | None) -> str:
    lines = [
        f"Finance Filing AI - what I filed for {hotel_name}",
        "",
        f"Filed automatically: {stats['filed']} invoice(s), EUR {stats['filed_total_eur']:.2f}.",
        f"Waiting for you: {stats['held']} invoice(s), EUR {stats['held_total_eur']:.2f} "
        f"({stats['queued']} more queued to auto-file once mode is live).",
    ]
    if stats["held_detail"]:
        lines.append("")
        lines.append("What needs a look:")
        for row in stats["held_detail"]:
            lines.append(f"  - {row['vendor']}, EUR {row['amount']}: {row['reason']}")
    if narrative:
        lines += ["", narrative]
    lines += ["", "Run `python3 tools/review.py list --kind invoice` for the full queue."]
    return "\n".join(lines)


def build_digest(settings, store: Store, *, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    with Run("daily-digest", settings, store) as run:
        last = store.get_cursor("last_digest_at")
        stats = gather_since(store, last)
        narrative = None
        if settings.agent_get("narrate.enabled", False):
            from narrate import build_narrative  # local import: optional dependency
            try:
                narrative = build_narrative(settings, store, stats)
            except LLMPendingInteractive:
                # Deliberately NOT caught below: a pending interactive prompt
                # must reach the user, never be swallowed as "no note this
                # time" - see core/llm.py's LLMPendingInteractive docstring.
                raise
            except Exception:  # noqa: BLE001 - any OTHER failure must never block the digest
                log.warn("controller's note skipped")
                narrative = None
        to = [settings.contacts.escalation_email] if settings.contacts.escalation_email else []
        body = build_body(settings.hotel.name, stats, narrative)
        payload = {"date": day, "to": to, "stats": stats}
        draft = {"subject": f"{settings.hotel.name} - invoice filing summary, {day}", "body": body}
        item, created = store.upsert_unique("digest", day, payload=payload, source="digest")
        if item.review_status in ("new", "pending_review"):
            # Only overwrite the draft while a human has not yet acted on it -
            # an approved/edited/sent digest keeps whatever a person decided.
            store.set_fields(item.id, draft=draft, intent="digest")
        if item.review_status == "new":
            item = store.transition(item.id, "pending_review", actor="agent",
                                    detail={"filed": stats["filed"], "held": stats["held"]})
        store.set_cursor("last_digest_at",
                         datetime.now(timezone.utc).isoformat(timespec="seconds"))
        run.stats = {"item_id": item.id, "created": created, **stats}
        log.info("digest built", item_id=item.id, filed=stats["filed"], held=stats["held"])
    return {"item_id": item.id, "created": created, "stats": stats}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        try:
            result = build_digest(settings, store, day=args.date)
        except LLMPendingInteractive as exc:
            print(str(exc))
            return 3
        stats = result["stats"]
        print(f"digest {result['item_id']} queued: {stats['filed']} filed "
             f"(EUR {stats['filed_total_eur']:.2f}), {stats['held']} held "
             f"(EUR {stats['held_total_eur']:.2f}).")
        print("Approve it with `python3 tools/review.py approve <id>`, then "
             "`python3 tools/review.py send`.")
        return 0
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
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
