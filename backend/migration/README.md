# SQLite migration -- foundation (schema + migration script only)

## Status: NOT complete. Do not merge expecting a working SQLite-backed app.

This PR lays the groundwork for moving off the single-JSON-blob store
(`storage/one-api.json`) to SQLite, per the follow-up notes from PR#1. It
contains:

- `schema.sql` -- a normalized SQLite schema for every collection currently
  in the JSON store (contacts, lists, tags, templates, campaigns,
  campaign_results, messages, conversations, automations, automation_runs,
  webhook_events, media, flows, phone_numbers, custom_fields, settings).
  `campaign_results` and `messages` get their own tables (not nested JSON)
  since they're the two fastest-growing collections -- this is the core of
  what actually fixes the "rewrite the whole file on every write" problem.
- `generate_synthetic_dataset.py` -- builds a throwaway JSON file shaped
  like production, at production scale (1724 contacts, 3700 messages, 13+
  campaigns with realistic result counts, webhook events, flows,
  automations). Never touches real data.
- `migrate_json_to_sqlite.py` -- the one-time migration script. Reads a
  JSON export in the same shape as `one-api.json` and loads it into a fresh
  SQLite database following `schema.sql`.
- `test_migration.py` -- validates the migration script against the
  synthetic dataset: exact row-count parity for every collection, field-
  level fidelity spot checks, referential integrity, and specific edge
  cases (contacts missing `name`/`customFields`, a campaign with zero
  results, duplicate-phone defensiveness). All 9 checks pass.

Run it yourself:

```bash
cd backend
source ../.venv/bin/activate  # or your own venv with backend/requirements.txt
python migration/generate_synthetic_dataset.py /tmp/synthetic.json
python migration/migrate_json_to_sqlite.py /tmp/synthetic.json /tmp/synthetic.db
python -m pytest migration/test_migration.py -v
```

## What this PR deliberately does NOT do

It does **not** touch `server.py`. None of the ~90 `store.read()`/
`store.write()` call sites across ~40 endpoints have been rewritten to use
SQL. The app still runs exactly as before, against the JSON blob -- this
PR is inert with respect to runtime behavior. That's intentional: rewriting
every endpoint category (contacts, campaigns, messages/inbox, webhooks,
flows, automations) while preserving *exact* existing behavior --
`scoped()` phoneNumberId scoping, `ensure_named_tag`/`ensure_named_list`
dedup, `summarize_campaign`, `update_campaign_delivery_from_status`,
`update_campaign_click_from_inbound` -- and verifying each category against
the current JSON-backed behavior before moving to the next, is a
multi-day effort in its own right. Attempting to compress that into the
time available before Thursday's campaign, on infrastructure a real sales
push depends on, would trade a well-understood performance problem (slow
under heavy concurrent load, but PR#1 + PR B already stop it from
deadlocking or losing data) for an untested one. That's a worse trade than
shipping PR A + PR B now and doing this properly afterward.

## Recommended next steps (separate, future effort)

1. Build a small compatibility shim so `store.read()` returns the same
   dict-of-lists shape server.py already expects, backed by SQL queries
   under the hood -- lets endpoints be migrated one at a time instead of
   all at once.
2. Migrate categories in this order, verifying each against the current
   JSON-backed behavior on the same synthetic dataset before moving on:
   contacts/lists/tags/custom-fields -> templates/flows/automations ->
   phone-numbers/settings -> messages/conversations/inbox -> campaigns/
   campaign_results -> webhooks (the highest-risk category, since it's
   also the one PR B just fixed a live race condition in).
3. Only after all categories pass, run the same load test PR#1 used to
   verify its fix (bulk campaign send + concurrent webhook burst against
   the full production-scale synthetic dataset), with a p50/p95/max
   health-check latency comparison table against the current JSON-blob
   baseline, included in that PR's description.
