# SQLite migration

## Status: application-layer rewrite is complete. Read before merging.

This moves the app off the single-JSON-blob store (`storage/one-api.json`)
onto SQLite, per PR#1's follow-up notes. Unlike the first version of this
PR (schema + migration script only), `server.py` has now been fully
rewritten: every one of the ~90 `store.read()`/`store.write()` call sites
across every endpoint now goes through `backend/db.py` (a SQLite data-access
layer) instead of reading/parsing the entire JSON file on every request.

### What's here

- `schema.sql` -- a normalized SQLite schema for every collection (contacts,
  lists, tags, templates, campaigns, campaign_results, messages,
  conversations, automations, automation_runs, webhook_events, media, flows,
  phone_numbers, custom_fields, settings). `campaign_results` and `messages`
  get dedicated, indexed tables since they're the two fastest-growing
  collections -- this is the core of what fixes the "rewrite the whole file
  on every write" problem.
- `generate_synthetic_dataset.py` -- builds a throwaway JSON file shaped
  like production, at production scale (1724 contacts, 3700 messages, 14
  campaigns with realistic result counts, webhook events, flows,
  automations). Never touches real data.
- `migrate_json_to_sqlite.py` -- the one-time migration script. Also wired
  into `server.py`'s startup: if `one-api.db` doesn't exist yet but a legacy
  `one-api.json` does, it auto-migrates on first boot, so an operator can't
  accidentally deploy this version and silently start with an empty store.
- `test_migration.py` -- 9 checks validating exact row-count parity,
  field-level fidelity, referential integrity, and edge cases (contacts
  missing `name`/`customFields`, a zero-result campaign, re-run
  idempotency) against the synthetic dataset. All pass.
- `../db.py` -- the SQLite data-access layer server.py now calls into.
- `../verification/` -- category-by-category comparison harness that runs
  the old JSON-backed server and this SQLite-backed server side by side
  against identical synthetic data and diffs every response. See its
  README for what it caught (3 real bugs, all fixed) and what remaining
  differences are understood synthetic-data artifacts rather than logic
  bugs.
- `../loadtest/` -- load test harness matching PR#1's own methodology
  (bulk campaign send + concurrent webhook burst), plus `RESULTS.md` with
  the honest before/after numbers.

### Run it yourself

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests/ migration/test_migration.py -v
```

## Honest status going into review

**Correctness:** extensively verified. Full endpoint rewrite preserves
`scoped()` phoneNumberId scoping semantics (including the subtle case where
contacts get an "OR in phoneNumberIds list" fallback that no other entity
type gets), tag/list dedup via `ensure_named_tag`/`ensure_named_list`,
campaign summarization, and webhook status/click propagation via a properly
indexed lookup (replacing a full Python scan of every campaign's results).
28 tests pass (unit + integration + migration validation), plus a dedicated
category-by-category comparison against the old JSON-backed server on
identical production-scale synthetic data (see `../verification/`).

**Performance:** genuinely mixed, and reported honestly in
`../loadtest/RESULTS.md`. `/api/settings` and `/api/health` are
dramatically fixed (60-100x faster under the same concurrent burst that
caused the original incident) -- this directly answers the reported
"9 seconds for 480 bytes" problem. `/api/inbox` and `/api/campaigns`, the
other two endpoints named in the original report, did **not** improve
under an aggressive synthetic concurrent-burst test, and were measurably
slower than the JSON baseline in that specific scenario -- even though a
single uncontended request to either is fast (108ms / 65ms). The load test
doc lays out what was tried (fixed a real N+1 query bug, widened the
thread pool, tuned SQLite's synchronous mode) and what's still suspected
(SQLite contention from a fresh connection per request against tables
under sustained write pressure) along with a concrete next step (a small
persistent read-connection pool) that wasn't attempted yet due to time.

**Recommendation:** this PR is safe to merge from a correctness standpoint,
but the performance story for `/api/inbox` and `/api/campaigns` under heavy
concurrent load is not yet a clean win and deserves one more look before
this is treated as fully resolving the original incident. Consider merging
once reviewed, but keep an eye on those two endpoints' behavior during the
first real campaign send after deploy.
