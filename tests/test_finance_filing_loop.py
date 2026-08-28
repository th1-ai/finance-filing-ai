"""Tests for the loop: the store FSM, the write guard, and the two-model-call
resumability trap (docs/how-it-works.md, "Resumable stages").

``_settings()`` never reads this repo's own `config/agent.yaml` or
`config/hotel.yaml` - it points `AGENT_CONFIG_DIR` at a tmp copy of the
shipped `.example.yaml` files instead, so a hotel's own edits never turn
`make test` red (factory/workflows/build-repo.md §5).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.config import load_settings, sub_data_dir  # noqa: E402
from core.llm import LLMPendingInteractive  # noqa: E402
from core.store import Store  # noqa: E402
from engine import process_invoice  # noqa: E402
from meter_feed import load_meter_rows  # noqa: E402
from po_ledger import load_po_ledger  # noqa: E402
import review  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures" / "inbound"


def _settings(monkeypatch, tmp_path, mode: str = "shadow", provider: str = "mock"):
    cfg_dir = tmp_path / "example_config"
    cfg_dir.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "config" / "hotel.example.yaml", cfg_dir / "hotel.yaml")
    shutil.copy(REPO_ROOT / "config" / "agent.example.yaml", cfg_dir / "agent.yaml")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    return load_settings(mode=mode, provider=provider)


def _load_msg(settings, fixture_id: str):
    from core.adapters.email_mock import MockEmail
    adapter = MockEmail(settings)
    return next(m for m in adapter.fetch_unread(limit=50) if m.id == fixture_id)


def _process(settings, store, fixture_id, **kwargs):
    msg = _load_msg(settings, fixture_id)
    po_ledger = kwargs.pop("po_ledger", None) or load_po_ledger(settings)
    meter_rows = kwargs.pop("meter_rows", None) or load_meter_rows(settings)
    return process_invoice(settings, store, msg, po_ledger=po_ledger, meter_rows=meter_rows,
                           **kwargs)


EXPECTED_OUTCOME = {
    # fixture_id: (needs_human, category_source)
    "invoice-01": (False, "known_vendor"),   # CleanNest, no PO, under threshold, approved
    "invoice-02": (True, "categorize"),      # PO variance, breaches both tolerances
    "invoice-03": (False, "known_vendor"),   # electricity, within ceiling
    "invoice-04": (True, "known_vendor"),    # water, over ceiling - challenged
    "invoice-05": (True, "categorize"),      # non-PO, >= threshold - retrospective PO
    "invoice-06": (False, "known_vendor"),   # Skyline SaaS, small, approved
    "invoice-07": (True, "categorize"),      # Sundry - always below the confidence gate
    "invoice-08": (False, "categorize"),     # PO match ok
    "invoice-09": (True, "known_vendor"),    # PO named but not in the ledger
}


def test_all_nine_fixtures_land_where_the_spec_says(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    for fixture_id, (needs_human, source) in EXPECTED_OUTCOME.items():
        item, did_work = _process(settings, store, fixture_id)
        assert did_work, fixture_id
        assert (item.review_status == "needs_human") == needs_human, \
            f"{fixture_id}: expected needs_human={needs_human}, got {item.review_status}"
        assert item.draft["category_source"] == source, fixture_id
    store.close()


def test_known_vendor_fast_path_never_asks_the_categorize_prompt(monkeypatch, tmp_path):
    """invoice-01 (CleanNest Supplies) is in known_vendors, so `categorize` must
    never be called - there is deliberately no fixtures/expected/categorize/
    invoice-01.json. If the code called it anyway, the `mock` provider would
    fall back to the schema's first enum value ("Housekeeping") and this
    would still pass by coincidence - the real check is `category_source`."""
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    item, _ = _process(settings, store, "invoice-01")
    assert item.draft["category_source"] == "known_vendor"
    assert item.intent == "Housekeeping"
    store.close()


def test_po_variance_breaching_both_tolerances_holds(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    item, _ = _process(settings, store, "invoice-02")
    assert item.review_status == "needs_human"
    assert item.draft["match"] == "variance"
    assert item.draft["variance_eur"] == 146.50
    store.close()


def test_po_named_but_not_in_ledger_holds(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    item, _ = _process(settings, store, "invoice-09")
    assert item.review_status == "needs_human"
    assert item.draft["match"] == "no_po"
    assert "PO-9999" in item.draft["reason"]
    store.close()


def test_low_confidence_sundry_always_needs_a_human(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    item, _ = _process(settings, store, "invoice-07")
    assert item.intent == "Sundry"
    assert item.confidence == 0.55
    assert item.review_status == "needs_human"
    store.close()


def test_dedup_skips_invoices_already_seen_on_a_previous_pass(tmp_path, monkeypatch):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    item = store.upsert_item("invoices", "invoice-01", kind="invoice", payload={"id": "invoice-01"})
    # A row still in `new` is NOT reported - it must be retried, not skipped.
    assert store.already_processed("invoices", ["invoice-01"]) == set()
    store.transition(item.id, "needs_human", actor="agent")
    assert store.already_processed("invoices", ["invoice-01", "invoice-02"]) == {"invoice-01"}
    store.close()


def test_idempotent_rerun_does_no_work_the_second_time(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "loop.db")
    _process(settings, store, "invoice-01")
    item, did_work = _process(settings, store, "invoice-01")
    assert did_work is False
    store.close()


def test_dry_run_writes_no_db_rows_and_is_safe_to_repeat(monkeypatch, tmp_path):
    """factory/workflows/build-repo.md §5: '--dry-run writes nothing: no DB
    rows, no files, no sequence increments'. Run twice on the same fresh
    fixtures - if a row were written, the second run's `store.upsert_item`
    (or an earlier persisted `new` row) would change `store.counts()`; a
    real bug here (e.g. a stray `store.upsert_item` before the dry-run
    branch) would show up as a non-empty `counts()` or, if unique_key
    handling were wrong, an IntegrityError on the second pass."""
    settings = _settings(monkeypatch, tmp_path, mode="live")
    settings.dry_run = True
    store = Store(settings, path=tmp_path / "dryrun.db")
    filed_dir = REPO_ROOT / "data" / "exports" / "filed"
    before = set(filed_dir.rglob("*.json")) if filed_dir.exists() else set()

    for _ in range(2):
        for fixture_id in EXPECTED_OUTCOME:
            item, did_work = _process(settings, store, fixture_id)
            assert did_work
            assert item.review_status in ("needs_human", "dispatched")

    assert store.counts() == {}, "a dry run must never write an items row"
    after = set(filed_dir.rglob("*.json")) if filed_dir.exists() else set()
    assert after == before, "a dry run must never write a filed invoice"
    store.close()


def test_shadow_blocks_auto_filing_even_at_full_confidence(monkeypatch, tmp_path):
    """mode: shadow is a true kill switch - even invoice-01 (known vendor,
    clearly under threshold, approved) never reaches auto_sent while shadow
    is on. It queues as pending_review instead, ready to file once live."""
    settings = _settings(monkeypatch, tmp_path, mode="shadow")
    store = Store(settings, path=tmp_path / "shadow.db")
    item, _ = _process(settings, store, "invoice-01")
    assert item.review_status == "pending_review"
    assert item.draft["gl_code"] == "6210"
    store.close()


def test_live_mode_auto_files_a_confident_invoice(monkeypatch, tmp_path):
    """The counterpart to the test above: in `mode: live`, the same invoice
    really files itself and logs a finances-sheet row - this is Finance
    Filing AI's one genuinely autonomous path (docs/how-it-works.md)."""
    settings = _settings(monkeypatch, tmp_path, mode="live")
    store = Store(settings, path=tmp_path / "live.db")
    item, _ = _process(settings, store, "invoice-01")
    assert item.review_status == "auto_sent"
    filed_path = Path(item.sent_message_id)
    # `finalize_invoice` writes through `sub_data_dir("exports")`, which
    # resolves against `AGENT_REPO_ROOT` - and every test in this module is
    # sandboxed there by conftest.py's autouse `_isolated_repo` fixture, not
    # against this repo's own REPO_ROOT. Checking REPO_ROOT here always
    # missed the real file; it only ever passed because a stale leftover
    # from an earlier, unrelated run happened to sit at that exact
    # deterministic path in this repo's real data/ - see factory/workflows/
    # build-repo.md §5 "tests never read the live config" for why the
    # sandbox exists at all.
    import os
    sandbox_root = Path(os.environ["AGENT_REPO_ROOT"])
    assert (sandbox_root / "data" / "exports" / filed_path).exists()
    store.close()


def test_interactive_retry_resumes_at_categorize_not_extract(monkeypatch, tmp_path):
    """THE regression test for the trap found in two sibling builds: with the
    `interactive` provider, `categorize` (the LATER call) can pend AFTER
    `extract` (the EARLIER call) already succeeded. A bulk
    `already_processed()` pre-filter must not then skip this item forever -
    it must stay `new` and be retried, and the retry must resume at
    `categorize`, never re-ask `extract`. See docs/how-it-works.md,
    "Resumable stages".

    invoice-07 (Unknown Traders Ltd) is the one fixture whose vendor is not
    in known_vendors, so it genuinely needs the `categorize` model call.
    """
    settings = _settings(monkeypatch, tmp_path, provider="interactive")
    store = Store(settings, path=tmp_path / "interactive.db")
    pending = sub_data_dir("pending")
    for stale in pending.glob("*invoice-07*"):
        stale.unlink()

    try:
        # Pass 1: extract has no answer yet - pends.
        try:
            _process(settings, store, "invoice-07")
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "extract-invoice-07"

        item = store.get_by_external("invoices", "invoice-07")
        assert item.review_status == "new"
        assert "invoice-07" not in store.already_processed("invoices", ["invoice-07"])

        # Answer extract.
        extract_answer = json.loads((REPO_ROOT / "fixtures" / "expected" / "extract" /
                                     "invoice-07.json").read_text())
        (pending / "extract-invoice-07.answer.json").write_text(
            json.dumps(extract_answer), encoding="utf-8")

        # Pass 2: extract resumes from the answer and succeeds; categorize
        # (unknown vendor - no known_vendors fast path) has no answer yet.
        try:
            _process(settings, store, "invoice-07")
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "categorize-invoice-07"

        item = store.get_by_external("invoices", "invoice-07")
        assert item.review_status == "new", \
            "extract succeeding must not move the item out of 'new'"
        assert item.payload.get("_extract_cache") == extract_answer
        assert "invoice-07" not in store.already_processed("invoices", ["invoice-07"]), \
            "a parked item must be retried on the next bulk pass, never skipped"
        # extract's prompt is consumed (renamed .used) and not re-created -
        # proof the cached value was used instead of asking again.
        assert not (pending / "extract-invoice-07.prompt.md").exists()
        assert (pending / "extract-invoice-07.answer.json.used").exists()

        # Answer categorize.
        cat_answer = json.loads((REPO_ROOT / "fixtures" / "expected" / "categorize" /
                                 "invoice-07.json").read_text())
        (pending / "categorize-invoice-07.answer.json").write_text(
            json.dumps(cat_answer), encoding="utf-8")

        # Pass 3: both caches present, deterministic engine runs to completion.
        item, did_work = _process(settings, store, "invoice-07")
        assert did_work
        assert item.review_status == "needs_human"  # Sundry, 0.55 < 0.90
        assert item.intent == "Sundry"
        assert item.draft["category_source"] == "categorize"
        assert "invoice-07" in store.already_processed("invoices", ["invoice-07"])
    finally:
        for f in pending.glob("*invoice-07*"):
            f.unlink()
        store.close()


def test_interactive_pass_parks_every_pending_prompt_not_just_the_first(monkeypatch, tmp_path):
    """SIMULATION.md Finding 5: `tools/run.py:one_pass` used to `return` on the
    very first `LLMPendingInteractive`, so a fresh 9-invoice batch took ~15
    separate `make run` round trips (one per model call) to clear. A single
    pass must now try every message and park a prompt for each one that needs
    it, so the whole demo batch takes as many passes as there are stages
    (extract, then categorize), not one pass per invoice."""
    import run as run_tool  # tools/run.py - REPO_ROOT/tools is already on sys.path (top of file)

    settings = _settings(monkeypatch, tmp_path, provider="interactive")
    store = Store(settings, path=tmp_path / "batch.db")
    pending_dir = sub_data_dir("pending")
    for f in pending_dir.glob("*invoice-0*"):
        f.unlink()

    try:
        code, stats = run_tool.one_pass(settings, store, limit=50, provider="interactive")
        assert code == 3
        # A totally fresh pass: every one of the 9 fixtures needs `extract`
        # (nothing has a cache yet) - a single pass must park all 9 prompts,
        # not stop after the first.
        extract_prompts = list(pending_dir.glob("extract-invoice-*.prompt.md"))
        assert len(extract_prompts) == 9, \
            f"expected all 9 extract prompts parked in one pass, got {len(extract_prompts)}"
        assert stats["processed"] == 0  # nothing finished - everything is parked
    finally:
        for f in pending_dir.glob("*invoice-0*"):
            f.unlink()
        store.close()


def test_sample_item_shows_marker_in_list_line_and_show(monkeypatch, tmp_path, capsys):
    """core/store.py tags an item read through a mock adapter outside `make
    demo` as `_sample` (`Item.is_sample`) - a human working the real queue
    must see that at a glance, in both `list` and `show`."""
    settings = _settings(monkeypatch, tmp_path)
    store = Store(settings, path=tmp_path / "sample.db")
    item = store.upsert_item("invoices", "sample-marker-1", kind="invoice",
                             payload={"from": "vendor@example.com", "_sample": True})
    store.set_fields(item.id, draft={"vendor": "Acme Linens", "amount_eur": 120.0,
                                    "action": "file"})
    item = store.get_item(item.id)
    assert item.is_sample

    capsys.readouterr()
    review._print_item_line(item)
    assert "[SAMPLE DATA]" in capsys.readouterr().out

    rc = review.cmd_show(store, SimpleNamespace(id=item.id))
    assert rc == 0
    assert "[SAMPLE DATA]" in capsys.readouterr().out
    store.close()
