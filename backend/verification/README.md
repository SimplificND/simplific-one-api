# Category-by-category behavioral verification (old JSON vs new SQLite)

`compare_categories.py` runs the OLD JSON-backed server and the NEW
SQLite-backed server side by side, both loaded with the identical
production-scale synthetic dataset (`backend/migration/generate_synthetic_dataset.py`),
and diffs their responses across every endpoint category: contacts,
lists/tags/custom-fields/templates, phone-numbers, flows/automations,
campaigns, messages/inbox, dashboard, settings, webhooks.

## How to run it

```bash
# Terminal 1: old server (checkout main before this PR, or use `git show
# origin/main:backend/server.py` into a scratch copy), pointed at a copy of
# the synthetic dataset as one-api.json
STORAGE_DIR=/path/to/old/storage python -m uvicorn server:app --port 8101

# Terminal 2: this PR's server, pointed at a copy of the SAME synthetic
# dataset (it will auto-migrate one-api.json -> one-api.db on first boot)
STORAGE_DIR=/path/to/new/storage python -m uvicorn server:app --port 8102

# Terminal 3
python backend/verification/compare_categories.py
```

Note: seed a dataset with **no campaigns in "running" status** for this
comparison (or flip any to "done" first) -- PR A's auto-resume-on-startup
feature will otherwise kick in and start sending real (mock-mode) messages
the moment either server boots, mutating state as a side effect and
confounding a read-only comparison. That behavior is intentional and is
covered by its own dedicated tests (`backend/tests/test_pr_a_auto_resume.py`),
not by this script.

## What this caught

This exercise found and fixed 3 real bugs before they could reach
production:
1. A missing trailing comma in `db_upsert_phone_number`'s INSERT statement
   turned a tuple into a plain string, causing every `POST /api/phone-numbers`
   call to crash with a 500.
2. A column/value ordering mismatch in that same statement, which (once the
   comma bug was fixed) would have silently written `created_at` /
   `synced_at` / `refreshed_at` to the wrong columns.
3. An internal SQL row-id (`_rowid`) that had leaked into the public
   `/api/campaigns` response as an extraneous field on every campaign
   result.

## Known, understood remaining differences

After the fixes above, all categories match once compared with two
specific normalizations (both implemented in `compare_categories.py`):
- **Null-tolerant**: a key present with a `null` value is treated as
  equivalent to the key being absent. Real consumers (including the React
  frontend) can't distinguish the two in practice, and the synthetic
  dataset generator sometimes writes an explicit `null` for a field that
  the real app's code paths would simply never set on that object at all
  (e.g. `lastInboundAt` on a conversation that never received an inbound
  message).
- **Order-insensitive for `tags`/`lists`/`phoneNumberIds`**: the real app
  always writes these pre-sorted (`attach_labels()` / `mark_contact_scope()`
  both do `sorted(set(...))`), but the synthetic generator assigns them via
  `random.sample()` without sorting -- a data-generation artifact, not a
  real behavioral difference.

Every mismatch remaining after those two normalizations was traced to a
specific simplification in the synthetic generator (e.g. it sets fields
like `source` on synthetic inbound messages, which the real webhook handler
never does) rather than to a bug in the rewritten endpoint logic.
