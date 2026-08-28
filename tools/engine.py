"""tools/engine.py - Finance Filing AI's capture-to-filing pipeline.

Two model calls, both cached across passes, everything else pure Python:

    extract()      raw invoice text -> structured fields (vendor, invoice_no,
                    amount, net, VAT, line count, PO reference).
    categorize()    only called when the vendor is not in config/agent.yaml:
                    known_vendors - picks one of six ledger categories.

Confidence is never a model output. It is looked up from `gl_map`/`sundry` in
config/agent.yaml, purely as a function of the category - see
docs/how-it-works.md, "The central design choice". Everything from the GL
lookup onward (the 90% gate, the PO tolerance match, the no-PO branches, the
filename) is a pure function over plain dicts, unit-tested directly in
tests/test_finance_filing_engine.py.

Shared by tools/run.py (the real loop) and tools/demo.py (the zero-credential
walkthrough), so both exercise exactly the same code path.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.adapters import get_sheets
from core.adapters.base import EmailMessage
from core.config import Settings, sub_data_dir
from core.llm import LLMResult, LLMSchemaError, complete
from core.log import get_logger
from core.review import WriteBlocked, assert_write_allowed
from core.store import Item, Store
from core.templates import build_prompt

log = get_logger("engine")

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


EXTRACT_SCHEMA = _schema("extract")
CATEGORIZE_SCHEMA = _schema("categorize")

FINANCE_SHEET = "finances"
FINANCE_HEADER = ["filed_at", "item_id", "vendor", "invoice_no", "category", "gl_code",
                   "gl_label", "amount_eur", "po_ref", "match", "action", "filed_name"]


# --------------------------------------------------------------------------
# pure helpers - each one is a unit test in tests/test_finance_filing_engine.py
# --------------------------------------------------------------------------
def slugify(text: str | None) -> str:
    """Vendor name -> filename-safe slug. Accented letters fold to ASCII."""
    normalised = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalised).strip("-").lower()
    return slug or "vendor"


def build_filename(invoice_date: str | None, vendor: str, invoice_no: str,
                   amount_eur: float) -> str:
    """``YYYY-MM-DD_vendor-slug_invoiceno_amount.json`` - see docs/how-it-works.md
    design decision 5 for why this ships a JSON record, not a renamed PDF."""
    date_part = invoice_date if re.match(r"^\d{4}-\d{2}-\d{2}$", str(invoice_date or "")) \
        else "0000-00-00"
    inv_part = re.sub(r"[^A-Za-z0-9-]+", "", str(invoice_no or "")) or "noref"
    return f"{date_part}_{slugify(vendor)}_{inv_part}_{amount_eur:.2f}.json"


def known_vendor_lookup(vendor: str | None, known_vendors: dict) -> dict | None:
    """Case-insensitive exact match against config/agent.yaml: known_vendors."""
    if not vendor:
        return None
    return (known_vendors or {}).get(vendor.strip().lower())


def gl_lookup(category: str, gl_map: dict, sundry: dict) -> tuple[str, str, float]:
    """``(code, label, confidence)``. A category not in ``gl_map`` (i.e. ``Sundry``)
    falls back to ``sundry`` - confidence there is always below the gate on purpose."""
    entry = (gl_map or {}).get(category)
    if entry is None:
        s = sundry or {}
        return str(s.get("code", "6900")), str(s.get("label", "Sundry")), \
            float(s.get("confidence", 0.55))
    return str(entry.get("code", "")), str(entry.get("label", "")), \
        float(entry.get("confidence", 0.0))


def _normalize_vendor_for_match(text: str | None) -> str:
    """Fold accents/case/punctuation so 'Baía Fresca Seafood, Lda.' and 'baia fresca
    seafood lda' compare equal. Reuses the same normalisation as :func:`slugify`
    (dashes instead of spaces) purely because it is already accent-safe and
    tested - the dashes themselves are irrelevant here, only equality is checked."""
    return slugify(text)


def three_way_match(amount_eur: float, po: dict | None, po_ref: str,
                    tolerance_pct: float, tolerance_eur: float, *,
                    vendor: str | None = None) -> dict:
    """Spec step 4: a variance must breach BOTH the percentage AND the euro
    tolerance to hold a payment - "whichever bites second". Before any of that,
    the PO's own vendor must match the invoice's vendor at all: a close amount
    against someone else's PO is the wrong PO, not a clean match, however small
    the price difference happens to be (SIMULATION.md Finding 1)."""
    if po is None:
        return {"match": "no_po", "action": "hold", "notes": [],
                "reason": f"Invoice names purchase order {po_ref}, but it is not in the PO "
                          f"ledger. Held for a person to find the right PO or request one."}
    po_vendor = str(po.get("vendor") or "")
    if po_vendor and vendor and \
            _normalize_vendor_for_match(po_vendor) != _normalize_vendor_for_match(vendor):
        return {"match": "vendor_mismatch", "action": "hold", "notes": [],
                "reason": f"Wrong PO: this PO belongs to {po_vendor}, but the invoice is from "
                          f"{vendor}. {po_ref} is held as a vendor mismatch, whatever the "
                          f"amount says - not a price problem."}
    po_amount = float(po.get("amount_eur") or 0)
    variance_eur = round(amount_eur - po_amount, 2)
    variance_pct = round((variance_eur / po_amount) * 100, 1) if po_amount else 0.0
    breach_pct = abs(variance_pct) > tolerance_pct
    breach_eur = abs(variance_eur) > tolerance_eur
    direction = "over" if variance_eur > 0 else "under"
    if breach_pct and breach_eur:
        return {"match": "variance", "action": "hold", "variance_eur": variance_eur,
                "variance_pct": variance_pct, "expected_eur": po_amount, "notes": [],
                "reason": f"Price variance: invoiced EUR {amount_eur:.2f} against {po_ref} at "
                          f"EUR {po_amount:.2f} - {variance_pct:+.1f}% (EUR {abs(variance_eur):.2f} "
                          f"{direction}) on '{po.get('description', '')}'. Above the "
                          f"{tolerance_pct}% / EUR {tolerance_eur:.0f} tolerance, so the payment "
                          f"stops here."}
    notes = []
    if breach_pct or breach_eur:
        only = "percentage" if breach_pct else "euro"
        notes.append(f"Logged for the vendor review, not held: the variance breaches only the "
                     f"{only} tolerance, not both.")
    return {"match": "ok", "action": "schedule", "variance_eur": variance_eur,
            "variance_pct": variance_pct, "expected_eur": po_amount, "notes": notes,
            "reason": f"Matches purchase order {po_ref} within tolerance (EUR {amount_eur:.2f} "
                      f"vs EUR {po_amount:.2f}, {variance_pct:+.1f}%). Clear to file."}


_WATER_RE = re.compile(r"water|água|agua", re.I)


def utility_check(vendor: str, amount_eur: float, meter_rows: list[dict], *,
                  window_days: int, tariff_eur_per_kwh: float, tariff_eur_per_m3: float,
                  tolerance_pct: float, known_vendors: dict) -> dict:
    """Spec step 5.1: sum the last ``window_days`` meter rows, compare the bill
    to the contracted ceiling. Utility type comes from the vendor record first,
    the old name regex only as a fallback - see docs/how-it-works.md decision 7."""
    known = known_vendor_lookup(vendor, known_vendors) or {}
    utility_type = known.get("utility_type") or ("water" if _WATER_RE.search(vendor or "") else "electricity")
    rows = sorted(meter_rows, key=lambda r: r.get("day_offset", 0), reverse=True)[:window_days]
    if utility_type == "water":
        units = sum(float(r.get("water_m3", 0)) for r in rows)
        tariff, unit_label = tariff_eur_per_m3, "m3"
    else:
        units = sum(float(r.get("kwh", 0)) for r in rows)
        tariff, unit_label = tariff_eur_per_kwh, "kWh"
    room_nights = sum(float(r.get("occupied_rooms", 0)) for r in rows)

    if units <= 0:
        return {"match": "no_po", "action": "hold", "notes": [],
                "reason": "No meter data available for the billing window."}

    expected_eur = round(units * tariff, 2)
    implied_rate = round(amount_eur / units, 3)
    over_pct = round(((amount_eur - expected_eur) / expected_eur) * 100, 1) if expected_eur else 0.0

    if over_pct > tolerance_pct:
        return {"match": "no_po_utility_flag", "action": "hold", "expected_eur": expected_eur,
                "notes": [],
                "reason": f"No PO - checked against the meter feed. {units:.0f} {unit_label} "
                          f"metered over the last {len(rows)} days puts the ceiling at "
                          f"EUR {expected_eur:.2f} on the contracted EUR {tariff}/{unit_label}; "
                          f"this bill implies EUR {implied_rate}/{unit_label}, {over_pct:+.1f}% "
                          f"over. Challenged, not paid."}

    notes = []
    if room_nights:
        per_room = round(units / room_nights, 1)
        notes.append(f"Consumption context: {room_nights:.0f} occupied room-night(s) in the "
                     f"window - {per_room} {unit_label} per room-night.")
    return {"match": "no_po_utility_ok", "action": "schedule", "expected_eur": expected_eur,
            "notes": notes,
            "reason": f"No PO - checked against the meter feed instead. {units:.0f} {unit_label} "
                      f"metered over the last {len(rows)} days puts the ceiling at "
                      f"EUR {expected_eur:.2f} on the contracted EUR {tariff}/{unit_label}; this "
                      f"bill implies EUR {implied_rate}/{unit_label}. Nothing above consumption, "
                      f"so nothing to challenge."}


def no_po_branch(vendor: str, amount_eur: float, threshold_eur: float,
                 approved_vendors: list[str]) -> dict:
    """Spec step 5.2/5.3: a small invoice from an approved vendor clears; anything
    else, or anything at/above the threshold, waits for a retrospective PO."""
    approved = (vendor or "").strip().lower() in {str(v).strip().lower() for v in approved_vendors}
    if amount_eur < threshold_eur and approved:
        return {"match": "no_po", "action": "schedule", "notes": [],
                "reason": f"No purchase order, and none required: EUR {amount_eur:.2f} is under "
                          f"the EUR {threshold_eur:.0f} no-PO threshold and {vendor} is on the "
                          f"approved-vendor list."}
    if amount_eur < threshold_eur:
        return {"match": "no_po", "action": "hold", "notes": [],
                "reason": f"EUR {amount_eur:.2f} is under the EUR {threshold_eur:.0f} no-PO "
                          f"threshold, but {vendor} is not on the approved-vendor list. Held "
                          f"for a retrospective PO."}
    return {"match": "no_po", "action": "hold", "notes": [],
            "reason": f"EUR {amount_eur:.2f} is at or above the EUR {threshold_eur:.0f} no-PO "
                      f"threshold. Held for a retrospective PO."}


# --------------------------------------------------------------------------
# invoice capture
# --------------------------------------------------------------------------
def invoice_to_dict(msg: EmailMessage) -> dict:
    """The fields the prompts and the store need from an inbound invoice email.

    ``raw_text`` is whatever text the email adapter handed over - a real PDF
    text-extraction step is a genuine feature to add first, not built here;
    see docs/how-it-works.md design decision 12 and docs/integrations.md."""
    extra = msg.extra or {}
    return {
        "id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
        "subject": msg.subject, "received_at": msg.received_at,
        "raw_text": extra.get("raw_text") or msg.body_text,
        "attachment_filename": extra.get("attachment_filename", ""),
        "currency": extra.get("currency") or "EUR",
    }


# --------------------------------------------------------------------------
# the two model calls
# --------------------------------------------------------------------------
def run_extract(settings: Settings, store: Store | None, item: Item, msg: EmailMessage,
                *, provider: str | None = None) -> dict:
    """``store=None`` (a dry run) skips the ``events`` audit row - see
    ``core.llm.complete``, which only records one when ``store`` is given."""
    prompt = build_prompt("extract", settings=settings, item=invoice_to_dict(msg), fixture_id=msg.id)
    result: LLMResult = complete("extract", prompt, EXTRACT_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id, fixture_id=msg.id)
    return result.data or {}


def run_categorize(settings: Settings, store: Store | None, item: Item, msg: EmailMessage,
                   extracted: dict, *, provider: str | None = None) -> dict:
    cat_item = {**invoice_to_dict(msg), "extracted": extracted}
    prompt = build_prompt("categorize", settings=settings, item=cat_item, fixture_id=msg.id)
    result: LLMResult = complete("categorize", prompt, CATEGORIZE_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id, fixture_id=msg.id)
    data = dict(result.data or {})
    effective_provider = provider or settings.llm.provider
    if effective_provider == "mock" and not result.cached:
        # core.llm's `mock` provider had no fixtures/expected/categorize/<id>.json
        # for this invoice (result.cached is only True for a real fixture match),
        # so it fell back to core.llm.schema_example()'s first enum value - a
        # placeholder, not a real guess. Left alone this reads as a confident,
        # correct answer once gl_lookup() attaches a fixed high GL confidence to
        # it (SIMULATION.md Finding 4). Never trust it: route to Sundry, whose
        # confidence is always below the gate, so the item lands on needs_human
        # instead of silently filing on a guess.
        data["category"] = "Sundry"
        data["_mock_unmatched"] = True
    return data


# --------------------------------------------------------------------------
# filing + the finances sheet - the two writes this agent makes
# --------------------------------------------------------------------------
def log_to_finances_sheet(settings: Settings, item: Item, draft: dict) -> dict:
    sheets = get_sheets(settings)
    existing = []
    try:
        existing = sheets.read(FINANCE_SHEET)
    except Exception:  # noqa: BLE001 - a broken read must not block filing
        log.warn("could not read finances sheet before append")
    rows = [] if existing else [FINANCE_HEADER]
    rows.append([
        datetime.now(timezone.utc).isoformat(timespec="seconds"), item.id,
        draft.get("vendor", ""), draft.get("invoice_no", ""), draft.get("category", ""),
        draft.get("gl_code", ""), draft.get("gl_label", ""), draft.get("amount_eur", ""),
        (draft.get("extracted") or {}).get("po_ref") or "", draft.get("match", ""),
        draft.get("action", ""), draft.get("filed_name", ""),
    ])
    return sheets.append(FINANCE_SHEET, rows, item=item)


def finalize_invoice(settings: Settings, item: Item) -> dict:
    """The one write path both the autonomous and the human-approved routes call.

    Guarded exactly like an adapter write (``assert_write_allowed`` is what
    ``@guarded_write`` calls under the hood - see core/adapters/base.py) even
    though filing itself is a local file write, not an adapter method: this is
    THE action ``mode: shadow`` and ``--dry-run`` must block."""
    assert_write_allowed(settings, "file_invoice", item)
    draft = item.draft or {}
    category = draft.get("category") or "Sundry"
    filename = draft.get("filed_name") or build_filename(
        (draft.get("extracted") or {}).get("invoice_date"), draft.get("vendor", ""),
        draft.get("invoice_no", ""), float(draft.get("amount_eur") or 0))
    target_dir = sub_data_dir("exports") / "filed" / category
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    record = {**draft, "item_id": item.id,
             "filed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    sheet_result = log_to_finances_sheet(settings, item, draft)
    return {"filed_path": str(target.relative_to(sub_data_dir("exports"))), "sheet": sheet_result}


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------
def process_invoice(settings: Settings, store: Store, msg: EmailMessage, *,
                    po_ledger: Any = None, meter_rows: list[dict] | None = None,
                    provider: str | None = None) -> tuple[Item, bool]:
    """Capture, code, match and (if it earns it) file one invoice.

    Idempotent and resumable: an item that already has a full ``draft`` was
    finished by an earlier pass and is left untouched (returns
    ``(item, False)``). One that only got as far as ``extract`` - or as far
    as ``extract`` and ``categorize`` - resumes at the next unfinished stage
    instead of re-asking a question the interactive provider already
    answered. The item is only moved out of ``review_status = 'new'`` once,
    at the very end, after the whole decision is computed - see
    docs/how-it-works.md, "Resumable stages", for why that ordering is load-
    bearing and not just tidy.

    ``settings.dry_run`` computes and returns the exact same decision but
    writes nothing: no ``items`` row, no ``_extract_cache``/``_category_cache``
    on a row, no ``events`` row for the model calls, and (because
    ``finalize_invoice`` is never even attempted) no filed JSON and no
    finances-sheet row. The one exception is the ``interactive`` provider's
    own ``data/pending/*.prompt.md`` file, which is how a dry run lets you
    preview the exact prompt without committing to anything else.
    """
    meter_rows = meter_rows or []
    payload = invoice_to_dict(msg)
    dry = settings.dry_run
    existing = store.get_by_external("invoices", msg.id)  # a read - always safe
    if existing is not None and existing.payload:
        for key in ("_extract_cache", "_category_cache"):
            if key in existing.payload:
                payload[key] = existing.payload[key]

    if dry:
        item = existing if existing is not None else Item(
            id=f"dry-run-{msg.id}", kind="invoice", source="invoices",
            external_id=msg.id, payload=payload, review_status="new")
        item.payload = payload
        call_store = None  # complete() only writes an `events` row when store is not None
    else:
        item = store.upsert_item("invoices", msg.id, kind="invoice", payload=payload)
        call_store = store

    if item.draft is not None:
        return item, False

    def _cache(extra: dict) -> None:
        """Merge ``extra`` into the item's payload - in memory only on a
        dry run, persisted otherwise. The only place either cache is written."""
        nonlocal item
        merged = {**(item.payload or {}), **extra}
        if dry:
            item.payload = merged
        else:
            item = store.set_fields(item.id, payload=merged) or item

    extracted = (item.payload or {}).get("_extract_cache")
    if not extracted:
        try:
            extracted = run_extract(settings, call_store, item, msg, provider=provider)
        except LLMSchemaError as exc:
            if dry:
                item.error, item.review_status = str(exc), "needs_human"
                return item, True
            store.set_fields(item.id, error=str(exc))
            updated = store.transition(item.id, "needs_human", actor="agent",
                                       detail={"error": "extract_schema_error"})
            return updated, True
        _cache({"_extract_cache": extracted})

    cached_category = (item.payload or {}).get("_category_cache")
    if cached_category:
        category = cached_category["category"]
        category_source = cached_category["source"]
    else:
        known = known_vendor_lookup(extracted.get("vendor"), settings.agent_get("known_vendors", {}))
        if known:
            category, category_source = known["category"], "known_vendor"
        else:
            try:
                result = run_categorize(settings, call_store, item, msg, extracted, provider=provider)
            except LLMSchemaError as exc:
                if dry:
                    item.error, item.review_status = str(exc), "needs_human"
                    return item, True
                store.set_fields(item.id, error=str(exc))
                updated = store.transition(item.id, "needs_human", actor="agent",
                                           detail={"error": "categorize_schema_error"})
                return updated, True
            category, category_source = result.get("category", "Sundry"), (
                "mock_unmatched_vendor" if result.get("_mock_unmatched") else "categorize")
        _cache({"_category_cache": {"category": category, "source": category_source}})

    # Deterministic from here on - never pends, never calls a model again.
    gl_map = settings.agent_get("gl_map", {})
    sundry = settings.agent_get("sundry", {})
    gl_code, gl_label, confidence = gl_lookup(category, gl_map, sundry)
    rules = settings.agent_get("rules", {}) or {}
    threshold = float(settings.agent_get("confidence_threshold", 0.90))

    if not rules.get("gl-auto", True):
        gl_code, gl_label, confidence = None, None, 0.0
        decision = {"match": None, "action": "hold", "notes": [],
                    "reason": "gl-auto is off: no ledger code assigned. Held anyway: with "
                              "auto GL coding off there is no ledger code to post against."}
    elif confidence < threshold:
        extra = (" This vendor is not in known_vendors and demo mode has no canned "
                 "categorize answer for it - never guessing a live category from a "
                 "placeholder." if category_source == "mock_unmatched_vendor" else "")
        decision = {"match": None, "action": "hold", "notes": [],
                    "reason": f"Coded {gl_label} at {confidence:.0%} confidence, below the "
                              f"{threshold:.0%} threshold - asking a human to confirm rather "
                              f"than guessing.{extra}"}
    else:
        po_ref = extracted.get("po_ref")
        amount_eur = float(extracted.get("amount_eur") or 0)
        if po_ref:
            po = po_ledger.find(po_ref) if po_ledger is not None else None
            decision = three_way_match(amount_eur, po, po_ref,
                                       float(settings.agent_get("matching.tolerance_pct", 2)),
                                       float(settings.agent_get("matching.tolerance_eur", 100)),
                                       vendor=extracted.get("vendor", ""))
        elif category == "Utilities" and rules.get("utility-anomaly", True):
            decision = utility_check(
                extracted.get("vendor", ""), amount_eur, meter_rows,
                window_days=int(settings.agent_get("utility.window_days", 30)),
                tariff_eur_per_kwh=float(settings.agent_get("utility.tariff_eur_per_kwh", 0.18)),
                tariff_eur_per_m3=float(settings.agent_get("utility.tariff_eur_per_m3", 2.15)),
                tolerance_pct=float(settings.agent_get("utility.tolerance_pct", 15)),
                known_vendors=settings.agent_get("known_vendors", {}))
        elif category == "Utilities":
            decision = {"match": "no_po", "action": "schedule", "notes": [
                "utility-anomaly is off: a runaway bill would clear unseen."],
                       "reason": "No PO, and utility-anomaly is off: queued on trust, no "
                                 "meter check run."}
        else:
            decision = no_po_branch(
                extracted.get("vendor", ""), amount_eur,
                float(settings.agent_get("matching.no_po_threshold_eur", 1000)),
                settings.agent_get("approved_vendors", []))

    filed_name = build_filename(extracted.get("invoice_date"), extracted.get("vendor", ""),
                               extracted.get("invoice_no", ""), float(extracted.get("amount_eur") or 0))
    draft = {
        "vendor": extracted.get("vendor", ""), "invoice_no": extracted.get("invoice_no", ""),
        "amount_eur": extracted.get("amount_eur"), "currency": payload.get("currency", "EUR"),
        "extracted": extracted, "category": category, "category_source": category_source,
        "gl_code": gl_code, "gl_label": gl_label, "filed_name": filed_name, **decision,
    }
    needs_human = confidence < threshold or draft["action"] == "hold" or gl_code is None

    if dry:
        # Compute and report the decision - never touch the store or the
        # filesystem. `review_status` here is what WOULD happen, not what did.
        item.intent, item.confidence, item.draft = category, confidence, draft
        item.review_status = "needs_human" if needs_human else "dispatched"
        log.info("computed (--dry-run, nothing written)", item_id=item.id,
                 vendor=draft["vendor"], would_be=item.review_status)
        return item, True

    store.set_fields(item.id, intent=category, confidence=confidence, draft=draft)

    if needs_human:
        updated = store.transition(item.id, "needs_human", actor="agent",
                                   detail={"reason": draft["reason"]})
        log.info("held", item_id=updated.id, vendor=draft["vendor"], reason=draft["reason"])
        return updated, True

    updated = store.transition(item.id, "dispatched", actor="agent",
                               detail={"gl_code": gl_code, "category": category})
    try:
        result = finalize_invoice(settings, updated)
    except WriteBlocked as exc:
        updated = store.transition(updated.id, "pending_review", actor="agent",
                                   detail={"reason": f"ready to auto-file once mode is live: {exc}"})
        log.info("queued (would auto-file once live)", item_id=updated.id, vendor=draft["vendor"])
        return updated, True
    store.set_fields(updated.id, sent_message_id=result.get("filed_path"))
    updated = store.transition(updated.id, "auto_sent", actor="agent", detail=result)
    log.info("auto-filed", item_id=updated.id, vendor=draft["vendor"], filed=result["filed_path"])
    return updated, True
