# Load test: JSON blob vs SQLite, under incident-matching conditions

Methodology matches PR#1's own verification: seed the store at production
scale (1724 contacts, 3700 messages, 14 campaigns -- see
`backend/migration/generate_synthetic_dataset.py`), then fire a concurrent
burst matching the incident conditions (a bulk campaign send + a burst of
Meta webhook delivery confirmations arriving concurrently), for 12 seconds,
while continuously probing 4 endpoints: `/api/health`, `/api/inbox`,
`/api/settings`, `/api/campaigns` -- the three named in the task plus health
as the original PR#1 baseline. Run with `run_load_test.py <label> <base_url>`
against a running server.

Both the OLD (JSON blob, current production code) and NEW (this PR's SQLite
backend) server were tested against byte-identical synthetic data, back to
back, on the same machine.

## Results

| Endpoint | OLD p50 | OLD p95 | OLD max | NEW p50 | NEW p95 | NEW max |
|---|---|---|---|---|---|---|
| `/api/health` | 1341 ms | 3290 ms | 3359 ms | **21 ms** | **62 ms** | 363 ms |
| `/api/settings` | 1609 ms | 3496 ms | 3562 ms | **16 ms** | **51 ms** | 356 ms |
| `/api/inbox` | 629 ms | 2792 ms | 2920 ms | 2094 ms | 2656 ms | 3082 ms |
| `/api/campaigns` | 866 ms | 3478 ms | 3515 ms | 2817 ms | 3849 ms | 3960 ms |

(NEW numbers are from the last tuning pass -- see "what was tried" below.
Full JSON output from every run is preserved in this PR's description /
session notes; `run_load_test.py` writes `/tmp/loadtest_result_<label>.json`
each time it's run.)

Throughput tells the same story from a different angle: over the 12s
window, OLD completed 27-48 requests per endpoint regardless of which
endpoint it was (everything is equally bottlenecked by the full-file
read+parse). NEW completed 1800-2800 requests for `/api/health` and
`/api/settings` in the same window, but only 17-35 for `/api/inbox` and
`/api/campaigns`.

## Honest read of these numbers

**`/api/settings` and `/api/health` are fixed, dramatically.** This is a
direct answer to the reported "9 seconds for 480 bytes" complaint: those
endpoints only ever needed the tiny `settings` table (or nothing at all),
and previously paid the cost of parsing the entire growing JSON file anyway.
Under this same concurrent burst, they're now 60-100x faster and handle
50-100x more throughput. This class of endpoint is fully solved by the
migration as implemented.

**`/api/inbox` and `/api/campaigns` did NOT improve under this specific,
aggressive synthetic burst -- and were measurably worse than the OLD
baseline in this run.** Measured in isolation (no concurrent burst), a
single request to either endpoint is fast (108ms / 65ms respectively) at
today's data volume. `EXPLAIN QUERY PLAN` on the `/api/inbox` "latest
message per conversation" query confirms it uses the covering index on
`messages(conversation_id)` (not a raw table scan), but it still has to
walk every row in the `messages` table once (4612 rows in this test) to
compute `MAX(rowid)` per conversation -- an O(total messages) cost on
*every single call*, independent of load. That's fine today; it will get
slower on its own as the messages table keeps growing over months of
production use, separate from the concurrency question below. A proper
fix would maintain a `last_message_id` column on `conversations` updated
incrementally on each insert (see `db_update_conversation_on_message`),
avoiding any need to scan `messages` at read time -- not implemented here
due to time, but a concrete, scoped follow-up.

Under concurrent load, the root cause of the slowdown is separate. The
regression only shows up under heavy concurrent access, specifically
because these two endpoints are the ones that read from the exact tables
(`messages`, `campaign_results`) that the concurrent bulk campaign send and
webhook burst are writing into. The leading hypothesis (not yet confirmed
with certainty) is SQLite-level contention from opening a fresh connection
per request against tables under sustained write pressure -- each new
reader connection has to reconcile against a growing WAL file during the
burst. Two things were tried and did not resolve it:
- Fixed a genuine N+1 query bug in campaign-result serialization (was
  issuing one extra query per result row -- up to hundreds per single
  `/api/campaigns` call); this measurably helped but didn't close the gap.
- Widened the asyncio default executor thread pool (was capped at ~12
  threads on this machine, likely too few for this concurrency level) and
  set `PRAGMA synchronous=NORMAL` (the standard WAL-mode pairing). Neither
  meaningfully changed the `/api/inbox` / `/api/campaigns` numbers.

## Recommended next step (not done here)

The next thing to try is a small pool of long-lived, reused connections
(rather than a fresh `sqlite3.connect()` per request) for the read paths
that touch `messages`/`campaign_results`, and/or explicit periodic
`PRAGMA wal_checkpoint` tuning. This is a scoped, well-understood follow-up
-- not a sign the migration's data model or correctness is wrong (every
category was verified byte-for-byte equivalent to the old JSON-backed
server in `backend/migration/` category comparisons), just that the
concurrency tuning for these two specific hot-table read paths needs one
more iteration before this PR should be considered a complete win across
all three originally-reported slow endpoints.
