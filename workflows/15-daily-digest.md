# Workflow: the daily digest

Objective: send the owner or controller a plain-text summary of what this
agent filed and what is waiting for them - the roster's "emails a daily
summary of what it filed."

## Steps

1. **Build the digest.**
   ```bash
   python3 tools/digest.py
   ```
   Gathers everything filed or held since the last digest (or, on the first
   run, everything the agent has ever seen), and queues it as a
   `kind="digest"` item - the same review queue as an invoice, needing the
   same approval before it sends. Running this again the same day updates
   the queued draft instead of creating a second one.

2. **See it.**
   ```bash
   python3 tools/review.py list --kind digest
   python3 tools/review.py show <id>
   ```
   The body lists what was auto-filed (count and value), what is waiting
   for a human (count, value, and the top reasons), and, if
   `narrate.enabled: true`, a short cosmetic paragraph from
   `tools/narrate.py` - never a word that changes a filing decision, see
   `docs/how-it-works.md`.

3. **Approve and send.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
   `send` is the same command `workflows/80-review.md` uses for invoices -
   it looks at each claimed item's `kind` and does the right thing
   (`email.send()` for a digest, filing for an invoice). Blocked in
   `mode: shadow`, like every other write.

4. **Schedule it.**
   ```bash
   python3 tools/schedule.py --all
   ```
   `config/agent.yaml: schedule.daily-digest` sets when
   (`morning` = 07:00 by default) - see `scheduler/` for the generated
   cron/launchd/systemd snippets.

## The optional controller's note

`tools/narrate.py` is the only place besides `extract`/`categorize` that
calls a model, and only for a cosmetic paragraph appended to the digest
body - it never sees an invoice's own text, never changes a ledger code or
a filing decision, and a failure there never blocks the digest itself. Off
by default (`config/agent.yaml: narrate.enabled: false`).

## Edge cases

- **Nothing to report.** The digest still queues, with all-zero counts -
  useful as a "the agent is alive" signal even on a quiet day.
- **`narrate.enabled: true` and `llm.provider: interactive`.** Building the
  digest can itself pend, exit code 3, the same as `tools/run.py` - answer
  the prompt in `data/pending/` and run `python3 tools/digest.py` again.
- **An approved digest before you re-run `python3 tools/digest.py`.** The
  digest builder never overwrites a draft once you have approved, edited or
  sent it that day - see `tools/digest.py:build_digest`.
