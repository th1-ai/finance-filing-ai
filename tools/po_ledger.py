"""tools/po_ledger.py - the purchase-order ledger invoices get matched against.

Not a core adapter (yet). ``core/adapters/base.py`` defines a ``Procurement``
stub family (`core.adapters.get_stub("procurement", settings)`) but it is a
pure interface - every method raises `AdapterNotImplemented`, and nothing in
this family has fixture-backed PO data behind it. This module ships the two
readers that actually work with zero credentials or a plain CSV/JSON export,
the same shape `tools/reviews_adapters.py` in `review-response-ai` uses for
the same reason - see docs/how-it-works.md, "Core requests", and this repo's
build report.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Settings, repo_root, sub_data_dir


@dataclass
class PoLedger:
    """A lookup table of purchase orders, keyed by PO reference."""

    rows: dict[str, dict] = field(default_factory=dict)

    def find(self, po_ref: str | None) -> dict | None:
        if not po_ref:
            return None
        return self.rows.get(str(po_ref).strip().upper())

    def __len__(self) -> int:
        return len(self.rows)


def _bool(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _index(records: list[dict]) -> PoLedger:
    rows: dict[str, dict] = {}
    for r in records:
        ref = str(r.get("po_ref") or r.get("id") or "").strip().upper()
        if not ref:
            continue
        rows[ref] = {
            "po_ref": ref, "vendor": str(r.get("vendor", "")),
            "amount_eur": float(r.get("amount_eur") or 0),
            "description": str(r.get("description", "")),
            "received": _bool(r.get("received", False)),
        }
    return PoLedger(rows=rows)


def load_po_ledger(settings: Settings) -> PoLedger:
    """Read the PO ledger named by ``config/agent.yaml: po_ledger.adapter``.

    ``mock`` reads ``fixtures/hotel/purchase-orders.json`` - what `make demo`
    and the tests use. ``csv`` reads ``data/imports/purchase_orders.csv`` -
    an export from your procurement or accounting system. Either way, a
    missing file returns an empty ledger rather than raising: an invoice
    naming a PO that cannot be found is a normal, handled outcome (see
    `tools/engine.py:three_way_match`), not a crash.
    """
    name = str(settings.agent_get("po_ledger.adapter", "mock") or "mock").lower()
    if name == "csv":
        path = sub_data_dir("imports") / "purchase_orders.csv"
        if not path.exists():
            return PoLedger()
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return _index([dict(row) for row in csv.DictReader(fh)])

    path = repo_root() / "fixtures" / "hotel" / "purchase-orders.json"
    if not path.exists():
        return PoLedger()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PoLedger()
    return _index(records if isinstance(records, list) else [])
