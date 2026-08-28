"""Pure-function tests for tools/engine.py - no store, no LLM, no I/O.

Every rule in docs/how-it-works.md is checked here directly, over plain
dicts, so a change to a tolerance or a wording is a one-line diff to spot.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from engine import (build_filename, gl_lookup, known_vendor_lookup, no_po_branch,  # noqa: E402
                    slugify, three_way_match, utility_check)

GL_MAP = {
    "Housekeeping": {"code": "6210", "label": "Housekeeping supplies", "confidence": 0.97},
    "F&B": {"code": "5110", "label": "F&B cost of sales", "confidence": 0.96},
    "Utilities": {"code": "6410", "label": "Utilities - power & water", "confidence": 0.99},
    "Property": {"code": "6310", "label": "Property & grounds maintenance", "confidence": 0.95},
    "Software": {"code": "6720", "label": "Software subscriptions", "confidence": 0.98},
}
SUNDRY = {"code": "6900", "label": "Sundry", "confidence": 0.55}
KNOWN_VENDORS = {
    "cleannest supplies": {"category": "Housekeeping"},
    "costa watt energia": {"category": "Utilities", "utility_type": "electricity"},
    "serra blue water co": {"category": "Utilities", "utility_type": "water"},
}


# --------------------------------------------------------------------------
# slugify / build_filename
# --------------------------------------------------------------------------
def test_slugify_folds_accents_and_punctuation():
    assert slugify("Baía Fresca Seafood, Lda.") == "baia-fresca-seafood-lda"


def test_slugify_empty_falls_back():
    assert slugify("") == "vendor"
    assert slugify(None) == "vendor"


def test_build_filename_shape():
    name = build_filename("2026-08-13", "Baía Fresca Seafood", "FS-6621", 1719.0)
    assert name == "2026-08-13_baia-fresca-seafood_FS-6621_1719.00.json"


def test_build_filename_bad_date_falls_back_to_placeholder():
    name = build_filename(None, "Vendor", "INV-1", 10.0)
    assert name.startswith("0000-00-00_")


# --------------------------------------------------------------------------
# known_vendor_lookup / gl_lookup
# --------------------------------------------------------------------------
def test_known_vendor_lookup_case_insensitive():
    assert known_vendor_lookup("CleanNest Supplies", KNOWN_VENDORS)["category"] == "Housekeeping"
    assert known_vendor_lookup("unknown vendor", KNOWN_VENDORS) is None
    assert known_vendor_lookup(None, KNOWN_VENDORS) is None


def test_gl_lookup_known_category():
    code, label, confidence = gl_lookup("Utilities", GL_MAP, SUNDRY)
    assert (code, label) == ("6410", "Utilities - power & water")
    assert confidence == 0.99


def test_gl_lookup_sundry_fallback_is_always_below_the_gate():
    code, label, confidence = gl_lookup("Sundry", GL_MAP, SUNDRY)
    assert (code, label, confidence) == ("6900", "Sundry", 0.55)
    assert confidence < 0.90


# --------------------------------------------------------------------------
# three_way_match - spec step 4: both tolerances must breach to hold
# --------------------------------------------------------------------------
def test_three_way_match_within_tolerance_clears():
    po = {"amount_eur": 640.00, "description": "Quarterly grounds maintenance"}
    result = three_way_match(648.00, po, "PO-2002", tolerance_pct=2, tolerance_eur=100)
    assert result["match"] == "ok"
    assert result["action"] == "schedule"
    assert result["notes"] == []


def test_three_way_match_breaching_both_holds():
    po = {"amount_eur": 1572.50, "description": "Fresh seafood delivery"}
    result = three_way_match(1719.00, po, "PO-1001", tolerance_pct=2, tolerance_eur=100)
    assert result["match"] == "variance"
    assert result["action"] == "hold"
    assert result["variance_eur"] == 146.50
    assert "PO-1001" in result["reason"]


def test_three_way_match_breaching_only_percent_clears_with_a_note():
    # 3% over, but only EUR 30 - breaches the percentage tolerance alone.
    po = {"amount_eur": 1000.00, "description": "x"}
    result = three_way_match(1030.00, po, "PO-1", tolerance_pct=2, tolerance_eur=100)
    assert result["match"] == "ok"
    assert result["action"] == "schedule"
    assert result["notes"] and "not held" in result["notes"][0]


def test_three_way_match_breaching_only_euros_clears_with_a_note():
    # EUR 150 over on a large PO is well under 2%, but breaches the euro floor.
    po = {"amount_eur": 20000.00, "description": "x"}
    result = three_way_match(20150.00, po, "PO-1", tolerance_pct=2, tolerance_eur=100)
    assert result["match"] == "ok"
    assert result["action"] == "schedule"
    assert result["notes"] and "not held" in result["notes"][0]


def test_three_way_match_po_not_found():
    result = three_way_match(410.00, None, "PO-9999", tolerance_pct=2, tolerance_eur=100)
    assert result["match"] == "no_po"
    assert result["action"] == "hold"
    assert "PO-9999" in result["reason"]


def test_three_way_match_close_amount_wrong_vendor_holds_not_clears():
    # SIMULATION.md Finding 1: PO-2002 really is GreenScape Grounds' PO, and the
    # amount is well within tolerance (648.00 vs 640.00) - but the invoice in hand
    # is from a different vendor entirely (Bergkaeserei Arlberg). A close amount
    # must never be enough to clear the wrong PO.
    po = {"amount_eur": 640.00, "vendor": "GreenScape Grounds",
         "description": "Quarterly grounds maintenance"}
    result = three_way_match(648.00, po, "PO-2002", tolerance_pct=2, tolerance_eur=100,
                             vendor="Bergkaeserei Arlberg")
    assert result["match"] == "vendor_mismatch"
    assert result["action"] == "hold"
    assert "PO belongs to" in result["reason"]
    assert "GreenScape Grounds" in result["reason"]


def test_three_way_match_same_vendor_different_case_and_accents_still_clears():
    # The normalisation must not turn a real match into a false mismatch.
    po = {"amount_eur": 640.00, "vendor": "Baía Fresca Seafood, Lda.",
         "description": "x"}
    result = three_way_match(648.00, po, "PO-1", tolerance_pct=2, tolerance_eur=100,
                             vendor="baia fresca seafood lda")
    assert result["match"] == "ok"
    assert result["action"] == "schedule"


# --------------------------------------------------------------------------
# utility_check - spec step 5.1
# --------------------------------------------------------------------------
METER_ROWS = [{"day_offset": -n, "kwh": 1550, "water_m3": 12, "occupied_rooms": 38}
             for n in range(1, 31)]


def test_utility_check_electricity_within_ceiling_clears():
    # 30 x 1550 kWh = 46500; ceiling at EUR 0.18/kWh = EUR 8370.00.
    result = utility_check("Costa Watt Energia", 8500.00, METER_ROWS, window_days=30,
                           tariff_eur_per_kwh=0.18, tariff_eur_per_m3=2.15, tolerance_pct=15,
                           known_vendors=KNOWN_VENDORS)
    assert result["match"] == "no_po_utility_ok"
    assert result["action"] == "schedule"
    assert result["expected_eur"] == 8370.0


def test_utility_check_water_over_ceiling_holds():
    # 30 x 12 m3 = 360; ceiling at EUR 2.15/m3 = EUR 774.00. EUR 950 is +22.7%.
    result = utility_check("Serra Blue Water Co", 950.00, METER_ROWS, window_days=30,
                           tariff_eur_per_kwh=0.18, tariff_eur_per_m3=2.15, tolerance_pct=15,
                           known_vendors=KNOWN_VENDORS)
    assert result["match"] == "no_po_utility_flag"
    assert result["action"] == "hold"
    assert "Challenged" in result["reason"]


def test_utility_check_no_meter_data_holds():
    result = utility_check("Costa Watt Energia", 500.00, [], window_days=30,
                           tariff_eur_per_kwh=0.18, tariff_eur_per_m3=2.15, tolerance_pct=15,
                           known_vendors=KNOWN_VENDORS)
    assert result["action"] == "hold"
    assert "No meter data" in result["reason"]


def test_utility_check_vendor_name_regex_fallback():
    # Not in known_vendors at all - detected as water purely by name.
    result = utility_check("Rio Water Utilities", 500.00, METER_ROWS, window_days=30,
                           tariff_eur_per_kwh=0.18, tariff_eur_per_m3=2.15, tolerance_pct=15,
                           known_vendors={})
    # 360 m3 x EUR 2.15 = EUR 774 ceiling; EUR 500 is comfortably under it.
    assert result["match"] == "no_po_utility_ok"


# --------------------------------------------------------------------------
# no_po_branch - spec step 5.2 / 5.3
# --------------------------------------------------------------------------
def test_no_po_branch_small_and_approved_clears():
    result = no_po_branch("CleanNest Supplies", 249.00, 1000, ["cleannest supplies"])
    assert result == {
        "match": "no_po", "action": "schedule", "notes": [],
        "reason": "No purchase order, and none required: EUR 249.00 is under the EUR 1000 "
                  "no-PO threshold and CleanNest Supplies is on the approved-vendor list.",
    }


def test_no_po_branch_small_but_not_approved_holds():
    result = no_po_branch("Some New Vendor", 249.00, 1000, ["cleannest supplies"])
    assert result["action"] == "hold"
    assert "not on the approved-vendor list" in result["reason"]


def test_no_po_branch_at_or_above_threshold_always_holds():
    result = no_po_branch("CleanNest Supplies", 1000.00, 1000, ["cleannest supplies"])
    assert result["action"] == "hold"
    assert "retrospective PO" in result["reason"]
