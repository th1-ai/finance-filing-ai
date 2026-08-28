# knowledge/

This folder is the agent's memory of your property. Most agents in this family
read these files before they answer anything, so the quality of what is in
here is the quality of what goes out - but see the note below for what that
means specifically for **Finance Filing AI**.

## For this agent specifically

`prompts/extract.md` and `prompts/categorize.md` - the only two model calls
this agent ever makes - do not read `knowledge/` at all. `property.md` and
`faq.md` can stay as the shipped `.example.md` files forever with no effect
on any filing decision (`make doctor` confirms this - see `tools/doctor.py`).
The one file here this agent DOES use is `signature.md`: it is appended to
the daily digest email (`tools/digest.py`, the only email this agent ever
sends) via `core.adapters.base.Email.with_signature()`. It is optional - no
`signature.example.md` ships, and a missing one just means the digest goes
out with no sign-off.

## What to put here

| File | What it holds |
|---|---|
| `property.md` | The facts. Rooms, times, prices, policies, directions, what is nearby. |
| `faq.md` | Questions guests actually ask, and the answers you actually give. |
| `signature.md` | The sign-off on outgoing email. Plain text. |

Copy the `.example.md` files, rename them without `.example`, and fill them in:

```bash
cp knowledge/property.example.md knowledge/property.md
cp knowledge/faq.example.md      knowledge/faq.md
```

`knowledge/*.md` is gitignored (the `.example.md` files are not), because your
property notes are yours.

## How to write it

**Write it the way you would brief a new receptionist.** Short sentences,
concrete facts, no marketing language. The agent will quote this material to
guests, so anything vague here becomes something vague in an email.

**Be specific about numbers and times.** "Check-in from 15:00" is usable.
"Check-in in the afternoon" is not.

**Say what you do NOT do.** "We have no parking; the nearest car park is X, about
EUR 15 a day" prevents a wrong answer far better than silence does.

**Keep prices dated.** "Breakfast EUR 18 per person (2026 rates)" tells the agent
and you when it is stale.

**One fact per line where you can.** It makes the agent's job easier and it makes
your job easier when something changes.

## Keeping it current

The agent is only as right as this folder. When a policy changes, change it here
first. A good habit: whenever you correct one of the agent's drafts in the review
queue, ask whether the correction belongs in `property.md`. If it does, the agent
stops making that mistake.

You can also ask your Claude Code session to do it:

> Read knowledge/property.md and the last ten items in the review queue. If any
> of my edits contradict what is in the file, tell me which line to change.
