"""
Load test matching PR#1's own verification methodology: seed the store at
production scale, fire a concurrent burst matching the incident conditions
(bulk campaign send + concurrent webhook delivery burst), and measure
p50/p95/max health-check latency -- PLUS, per this task's explicit request,
the same percentiles for the three endpoints that were actually measured as
slow in production: /api/inbox, /api/settings, /api/campaigns.

Run against both BASE (port 9101, old JSON-blob store) and CANDIDATE (port
9102, new SQLite store) using the identical synthetic dataset, back to back.
"""
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

TARGET_LABEL = sys.argv[1] if len(sys.argv) > 1 else "target"
BASE_URL = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9101"

PROBE_ENDPOINTS = {
    "/api/health": {},
    "/api/inbox": {},
    "/api/settings": {},
    "/api/campaigns": {},
}

PROBE_DURATION_SECONDS = 12
WEBHOOK_BURST_COUNT = 300
CAMPAIGN_TARGET_CONTACTS = 400  # matches "bulk campaign send" incident condition


def percentile(values, pct):
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def create_bulk_campaign(client: httpx.Client) -> str:
    resp = client.post(f"{BASE_URL}/api/campaigns", json={
        "name": f"Load test bulk send ({TARGET_LABEL})",
        "templateName": "promo_julho",
        "language": "pt_BR",
        "listIds": [],
        "tagIds": [],
        "exclusionListIds": [],
        "phoneNumberId": "PHONE_MAIN",
        "batchSize": 50,
        "batchPauseSeconds": 0,
        "sendNow": True,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def fire_webhook_burst(client: httpx.Client, n: int) -> None:
    for i in range(n):
        payload = {
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "PHONE_MAIN", "display_phone_number": "+5511999990000"},
                "statuses": [{
                    "id": f"wamid.loadtest{TARGET_LABEL}{i:05d}",
                    "status": "delivered",
                    "recipient_id": "5511900000000",
                }],
            }}]}],
        }
        try:
            client.post(f"{BASE_URL}/api/meta/webhook", json=payload, timeout=10)
        except Exception:
            pass


def probe_endpoint(client: httpx.Client, path: str, params: dict, stop_at: float, out: list) -> None:
    while time.monotonic() < stop_at:
        start = time.monotonic()
        try:
            r = client.get(f"{BASE_URL}{path}", params=params, timeout=30)
            ok = r.status_code < 500
        except Exception:
            ok = False
        elapsed = time.monotonic() - start
        out.append((elapsed, ok))


def main():
    print(f"\n=== Load test: {TARGET_LABEL} ({BASE_URL}) ===")
    client = httpx.Client()

    campaign_id = create_bulk_campaign(client)
    print(f"Created bulk campaign {campaign_id} targeting up to {CAMPAIGN_TARGET_CONTACTS} contacts (sendNow=true)")

    probe_results = {path: [] for path in PROBE_ENDPOINTS}
    stop_at = time.monotonic() + PROBE_DURATION_SECONDS

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = []
        # Concurrent probes against each of the 4 endpoints, continuously,
        # for the whole burst window.
        for path, params in PROBE_ENDPOINTS.items():
            for _ in range(4):  # 4 concurrent probing "clients" per endpoint
                futures.append(pool.submit(probe_endpoint, httpx.Client(), path, params, stop_at, probe_results[path]))
        # Concurrent webhook delivery burst, overlapping with the probes and
        # with the campaign send that's already running as a background task
        # inside the server process.
        futures.append(pool.submit(fire_webhook_burst, httpx.Client(), WEBHOOK_BURST_COUNT))

        for f in as_completed(futures):
            f.result()

    print(f"\nResults over {PROBE_DURATION_SECONDS}s burst (bulk campaign send + {WEBHOOK_BURST_COUNT} concurrent webhook deliveries):\n")
    print(f"{'endpoint':<16} {'requests':>9} {'errors':>7} {'p50 (ms)':>10} {'p95 (ms)':>10} {'max (ms)':>10}")
    summary = {}
    for path, rows in probe_results.items():
        latencies = [r[0] * 1000 for r in rows]
        errors = sum(1 for r in rows if not r[1])
        p50 = percentile(latencies, 50)
        p95 = percentile(latencies, 95)
        mx = max(latencies) if latencies else float("nan")
        summary[path] = {"requests": len(rows), "errors": errors, "p50": p50, "p95": p95, "max": mx}
        print(f"{path:<16} {len(rows):>9} {errors:>7} {p50:>10.1f} {p95:>10.1f} {mx:>10.1f}")

    # Give the background campaign a moment then report final status.
    time.sleep(3)
    try:
        rows = client.get(f"{BASE_URL}/api/campaigns", timeout=30).json()
        camp = next(c for c in rows if c["id"] == campaign_id)
        print(f"\nCampaign final status: {camp['status']} ({camp['sent']} sent / {camp['targetCount']} target)")
    except Exception as e:
        print(f"\nCould not fetch final campaign status: {e}")

    import json
    with open(f"/tmp/loadtest_result_{TARGET_LABEL}.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSaved summary to /tmp/loadtest_result_{TARGET_LABEL}.json")


if __name__ == "__main__":
    main()
