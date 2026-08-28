"""tools/meter_feed.py - daily kWh/m3/occupancy rows for the utility cross-check.

The same honest-reader shape as `tools/po_ledger.py`: not a core adapter,
reference data `tools/engine.py:utility_check` reads, not a work item. See
docs/how-it-works.md, "Core requests".
"""

from __future__ import annotations

import csv
import json

from core.config import Settings, repo_root, sub_data_dir


def _num(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalise(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "day_offset": int(_num(r.get("day_offset"), 0)),
            "kwh": _num(r.get("kwh")),
            "water_m3": _num(r.get("water_m3")),
            "occupied_rooms": _num(r.get("occupied_rooms")),
        })
    return out


def load_meter_rows(settings: Settings) -> list[dict]:
    """Read the meter feed named by ``config/agent.yaml: meter_feed.adapter``.

    ``mock`` reads ``fixtures/hotel/meter-readings.json``. ``csv`` reads
    ``data/imports/meter_readings.csv`` - an export from your BMS or utility
    portal. A missing file returns an empty list; `utility_check` treats "no
    meter data" as a normal hold reason, not an error.
    """
    name = str(settings.agent_get("meter_feed.adapter", "mock") or "mock").lower()
    if name == "csv":
        path = sub_data_dir("imports") / "meter_readings.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return _normalise([dict(row) for row in csv.DictReader(fh)])

    path = repo_root() / "fixtures" / "hotel" / "meter-readings.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _normalise(rows if isinstance(rows, list) else [])
