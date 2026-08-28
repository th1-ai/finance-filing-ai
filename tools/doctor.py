#!/usr/bin/env python3
"""tools/doctor.py - is Finance Filing AI configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus this
agent's own: the ledger map, the known-vendor table, the PO ledger, the
meter feed, and the prompt files. Exits 0 when everything passed, 1 when a
FAIL line needs fixing. Never a traceback: a config error is shown as a FAIL
row like any other.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings, repo_root  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402
from meter_feed import load_meter_rows  # noqa: E402
from po_ledger import load_po_ledger  # noqa: E402


def check_gl_map(settings: Settings) -> Check:
    gl_map = settings.agent_get("gl_map", {})
    sundry = settings.agent_get("sundry", {})
    if not gl_map or not sundry:
        return Check("gl map", FAIL, "gl_map or sundry is missing from config/agent.yaml",
                     "Copy config/agent.example.yaml to config/agent.yaml - it ships with "
                     "five categories and a Sundry fallback.")
    threshold = float(settings.agent_get("confidence_threshold", 0.90))
    below = [c for c, e in gl_map.items() if float(e.get("confidence", 0)) < threshold]
    if below:
        return Check("gl map", WARN,
                     f"{', '.join(below)} sit below confidence_threshold ({threshold:.0%}) - "
                     f"every invoice in that category will always need a human")
    return Check("gl map", PASS, f"{len(gl_map)} categories, Sundry fallback at "
                 f"{float(sundry.get('confidence', 0)):.0%}")


def check_known_vendors(settings: Settings) -> Check:
    known = settings.agent_get("known_vendors", {})
    if not known:
        return Check("known vendors", WARN, "empty - every invoice will call the categorize "
                     "model", "Add vendors to config/agent.yaml: known_vendors to skip the "
                     "model call for suppliers you already recognise.")
    return Check("known vendors", PASS, f"{len(known)} vendor(s) coded without a model call")


def check_po_ledger(settings: Settings) -> Check:
    ledger = load_po_ledger(settings)
    adapter = settings.agent_get("po_ledger.adapter", "mock")
    if not len(ledger):
        return Check("po ledger", WARN, f"0 purchase orders ({adapter} adapter)",
                     "Fine if you have none yet. See docs/integrations.md#po-ledger.")
    return Check("po ledger", PASS, f"{len(ledger)} purchase order(s) ({adapter} adapter)")


def check_meter_feed(settings: Settings) -> Check:
    rows = load_meter_rows(settings)
    adapter = settings.agent_get("meter_feed.adapter", "mock")
    if not rows:
        return Check("meter feed", WARN, f"0 rows ({adapter} adapter) - a no-PO utility "
                     "invoice will always be held with 'no meter data available'",
                     "Fine until you connect a real utility feed. See "
                     "docs/integrations.md#meter-feed.")
    return Check("meter feed", PASS, f"{len(rows)} day(s) of readings ({adapter} adapter)")


def override_knowledge_check(checks: list[Check]) -> None:
    """Replace core.doctor's generic ``knowledge`` line (in place) with the truth
    for THIS agent (SIMULATION.md Finding 3).

    ``core/doctor.py:check_knowledge`` is shared across the whole family and
    says "the agent relies on it for every decision it takes" - false here.
    Grepping ``prompts/extract.md`` and ``prompts/categorize.md`` (this agent's
    only two model calls) shows neither one reads ``knowledge/`` at all, so
    ``property.md`` / ``faq.md`` can stay as the shipped ``.example.md`` files
    forever with no effect on any decision. The one file under ``knowledge/``
    this agent DOES use is ``signature.md`` - ``core.adapters.base.Email.
    with_signature()`` appends it to the daily digest email (the only email
    this agent ever sends; see ``tools/digest.py``, ``docs/safety.md``). It is
    entirely optional: a missing ``signature.md`` just means the digest goes
    out with no sign-off.
    """
    for i, check in enumerate(checks):
        if check.name != "knowledge":
            continue
        sig = repo_root() / "knowledge" / "signature.md"
        if sig.exists():
            checks[i] = Check("knowledge", PASS,
                              "signature.md present - appended to the daily digest email. "
                              "property.md/faq.md are NOT read by this agent (extract.md and "
                              "categorize.md never reference knowledge/) - fine to leave as "
                              "the shipped examples.")
        else:
            checks[i] = Check(
                "knowledge", PASS,
                "no knowledge/signature.md - the daily digest email will send with no "
                "sign-off, which is fine. property.md/faq.md are NOT read by this agent "
                "(extract.md and categorize.md never reference knowledge/) - nothing to "
                "fill in there.",
                "Optional: copy knowledge/signature.md into place yourself (no "
                "signature.example.md ships) if you want a sign-off on the digest email.")
        return


def check_prompts() -> Check:
    missing = [p for p in ("prompts/extract.md", "prompts/categorize.md", "prompts/narrate.md",
                           "prompts/schemas/extract.json", "prompts/schemas/categorize.json",
                           "prompts/schemas/narrate.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "extract.md + categorize.md + narrate.md + schemas present")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="Finance Filing AI - doctor")

    checks = run_checks(settings, extra=[check_gl_map, check_known_vendors, check_po_ledger,
                                         check_meter_feed])
    override_knowledge_check(checks)
    checks.append(check_prompts())
    return print_table(checks, title="Finance Filing AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
