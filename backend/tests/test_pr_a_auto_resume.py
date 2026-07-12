"""
Verification for PR A: campaigns stuck in "running" status must be
auto-resumed when the server process starts (e.g. after a crash/restart),
without any manual call to POST /api/campaigns/{id}/resume.

This launches the real ASGI app as a subprocess (uvicorn) against a seeded
JSON store, to faithfully simulate a container restart -- not just an
in-process TestClient, which wouldn't exercise a fresh process startup the
same way production restarts do.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_contact(i: int) -> dict:
    return {
        "id": f"lead_test{i:04d}",
        "name": f"Contact {i}",
        "phone": f"55119{i:08d}",
        "lastPhoneNumberId": "PHONE_A",
        "phoneNumberIds": ["PHONE_A"],
        "tags": [],
        "lists": ["list_all"],
        "customFields": {},
        "createdAt": "2026-07-01T00:00:00+00:00",
        "updatedAt": "2026-07-01T00:00:00+00:00",
    }


def seed_store(path: Path, total_contacts: int, already_processed: int, campaign_id: str) -> None:
    contacts = [make_contact(i) for i in range(total_contacts)]
    results = []
    for i in range(already_processed):
        c = contacts[i]
        results.append({
            "contactId": c["id"],
            "name": c["name"],
            "phone": c["phone"],
            "status": "sent",
            "messageIds": [f"msg_seed{i}"],
            "providerMessageIds": [],
            "sentAt": "2026-07-01T00:00:00+00:00",
            "deliveredAt": None,
            "readAt": None,
            "clickedAt": None,
            "buttonText": None,
            "error": None,
            "errorText": None,
            "diagnostic": {},
            "createdAt": "2026-07-01T00:00:00+00:00",
        })
    campaign = {
        "id": campaign_id,
        "name": "Stuck campaign",
        "templateName": "promo_template",
        "language": "pt_BR",
        "phoneNumberId": "PHONE_A",
        "responseFlowId": None,
        "scheduledAt": None,
        "targetCount": total_contacts,
        "sent": already_processed,
        "failed": 0,
        "delivered": 0,
        "read": 0,
        "buttonClicks": 0,
        "results": results,
        "lastError": None,
        "config": {
            "name": "Stuck campaign",
            "templateName": "promo_template",
            "language": "pt_BR",
            "listIds": ["list_all"],
            "tagIds": [],
            "exclusionListIds": [],
            "responseFlowId": None,
            "buttonFlowMap": {},
            "parameterMap": {},
            "phoneNumberId": "PHONE_A",
            "batchSize": 25,
            "batchPauseSeconds": 0,
            "sendNow": True,
            "scheduledAt": None,
        },
        "status": "running",
        "createdAt": "2026-07-01T00:00:00+00:00",
        "startedAt": "2026-07-01T00:00:00+00:00",
        "lastProgressAt": "2026-07-01T00:00:00+00:00",
    }
    store = {
        "contacts": contacts,
        "lists": [{"id": "list_all", "name": "All", "phoneNumberId": "PHONE_A", "createdAt": "2026-07-01T00:00:00+00:00"}],
        "tags": [],
        "templates": [{
            "id": "tpl_promo",
            "name": "promo_template",
            "language": "pt_BR",
            "category": "MARKETING",
            "bodyPreview": "Hello!",
            "phoneNumberId": "PHONE_A",
            "createdAt": "2026-07-01T00:00:00+00:00",
        }],
        "campaigns": [campaign],
        "conversations": [],
        "messages": [],
        "automations": [],
        "automationRuns": [],
        "webhookEvents": [],
        "media": [],
        "flows": [],
        "phoneNumbers": [{
            "id": "PHONE_A", "phoneNumberId": "PHONE_A", "displayPhoneNumber": "+5511999999999",
            "verifiedName": "Test", "qualityRating": "GREEN", "messagingLimitTier": "TIER_1K",
            "active": True, "source": "manual", "createdAt": "2026-07-01T00:00:00+00:00",
        }],
        "customFields": [],
        "settings": {},
    }
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def run_server_and_wait(storage_dir: Path, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "STORAGE_DIR": str(storage_dir),
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        # Intentionally leave META_ACCESS_TOKEN / META_PHONE_NUMBER_ID unset so
        # meta_send() takes the "mock" path (no real network calls), matching
        # how PR#1's own tests exercised send logic offline.
    }
    env.pop("META_ACCESS_TOKEN", None)
    env.pop("META_PHONE_NUMBER_ID", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
            if r.status_code == 200:
                return proc
        except Exception as e:
            last_err = e
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace")
            raise RuntimeError(f"Server exited early:\n{out}")
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"Server did not become healthy in time: {last_err}")


def poll_campaign(port: int, campaign_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = httpx.get(f"http://127.0.0.1:{port}/api/campaigns", timeout=5)
        r.raise_for_status()
        rows = r.json()
        last = next((c for c in rows if c["id"] == campaign_id), None)
        if last and last.get("status") in {"done", "failed"}:
            return last
        time.sleep(0.3)
    return last


def test_auto_resume_partial_campaign(tmp_path):
    """Campaign stuck at partial progress must finish automatically on startup,
    with no call to /resume, and without re-sending already-processed contacts."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    campaign_id = "camp_stuck1"
    total = 40
    already = 15
    seed_store(storage_dir / "one-api.json", total, already, campaign_id)

    port = free_port()
    proc = run_server_and_wait(storage_dir, port)
    try:
        result = poll_campaign(port, campaign_id, timeout=25)
        assert result is not None, "campaign disappeared"
        assert result["status"] == "done", f"expected done, got {result['status']}"
        assert result["targetCount"] == total
        assert len(result["results"]) == total, "all contacts should have exactly one result row"
        # No duplicate processing of already-processed contacts:
        contact_ids = [row["contactId"] for row in result["results"]]
        assert len(contact_ids) == len(set(contact_ids)), "duplicate result rows -- contact processed twice"
        assert result["sent"] == total
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_auto_resume_fully_processed_campaign_marks_done(tmp_path):
    """Edge case: every contact already has a result row (e.g. crash happened
    right after the last batch persisted but before the final status flip).
    Startup auto-resume must mark it 'done', not error or loop forever."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    campaign_id = "camp_stuck2"
    total = 10
    seed_store(storage_dir / "one-api.json", total, total, campaign_id)

    port = free_port()
    proc = run_server_and_wait(storage_dir, port)
    try:
        result = poll_campaign(port, campaign_id, timeout=15)
        assert result is not None
        assert result["status"] == "done"
        assert len(result["results"]) == total
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_no_running_campaigns_startup_is_a_noop(tmp_path):
    """Sanity check: startup scan must not error or hang when there are no
    campaigns in 'running' status."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    (storage_dir / "one-api.json").write_text(json.dumps({
        "contacts": [], "lists": [], "tags": [], "templates": [], "campaigns": [],
        "conversations": [], "messages": [], "automations": [], "automationRuns": [],
        "webhookEvents": [], "media": [], "flows": [], "phoneNumbers": [], "customFields": [],
        "settings": {},
    }), encoding="utf-8")
    port = free_port()
    proc = run_server_and_wait(storage_dir, port)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=5)
        assert r.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)
