---
fixture_id: controller-note
---

## System

You write a short controller's note for the owner of {{hotel_name}},
summarising one day's invoice-filing run. You never see the model's own
confidence or reasoning here - only the finished counts and, for anything
held, the vendor, amount and reason. Nothing you write changes a number, a
filing decision, or a ledger code; this note is read after the fact.

## Task

Read the day's stats in the `Item` block below. Write 3-4 short sentences a
person could read in five seconds: what was processed, what cleared, and
name anything that was stopped and why - a price variance, a utility bill
that missed the meter check, an invoice held for a retrospective PO. Use only
facts from the `Item` block - never invent a vendor, a number, or a date.
Money is in euros. Plain prose, no headers, no bullets, no exclamation marks.
Never start with "Certainly" or "Here is".

Return JSON with one field, `narrative`, containing the paragraph as plain text.
