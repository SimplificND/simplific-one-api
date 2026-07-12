"""
Broad smoke test for PR B: since the fix mechanically touched ~40 endpoints
(wrapping their read-modify-write cycle in STORE_MUTATION_LOCK), this
exercises each touched endpoint category end-to-end against a real running
uvicorn process, to catch any accidental behavior change (e.g. a stray
indent, a variable used after the lock block incorrectly, a raised
exception not propagating status codes correctly).
"""
import json
import os
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


def run_server(storage_dir: Path, port: int) -> subprocess.Popen:
    env = {**os.environ, "STORAGE_DIR": str(storage_dir), "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}"}
    env.pop("META_ACCESS_TOKEN", None)
    env.pop("META_PHONE_NUMBER_ID", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                return proc
        except Exception:
            pass
        if proc.poll() is not None:
            raise RuntimeError(proc.stdout.read().decode(errors="replace"))
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("server did not start")


def test_full_endpoint_smoke(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    port = free_port()
    proc = run_server(storage_dir, port)
    base = f"http://127.0.0.1:{port}"
    try:
        # tags / lists / custom fields
        tag = httpx.post(f"{base}/api/tags", json={"name": "VIP", "color": "#ff0000"}, timeout=5).json()
        assert tag["name"] == "VIP"
        lst = httpx.post(f"{base}/api/lists", json={"name": "Leads Q3"}, timeout=5).json()
        assert lst["name"] == "Leads Q3"
        cf = httpx.post(f"{base}/api/custom-fields", json={"key": "cidade", "label": "Cidade"}, timeout=5).json()
        assert cf["key"] == "cidade"

        # contacts
        contact = httpx.post(f"{base}/api/contacts", json={
            "phone": "5511988887777", "name": "Ana", "tags": ["VIP"], "lists": ["Leads Q3"], "customFields": {"cidade": "SP"},
        }, timeout=5).json()
        assert contact["phone"] == "5511988887777"
        assert set(httpx.get(f"{base}/api/contacts", timeout=5).json()[0]["tags"]) == set(contact["tags"])

        updated = httpx.patch(f"{base}/api/contacts/{contact['id']}", json={
            "name": "Ana Paula", "tags": contact["tags"], "lists": contact["lists"], "customFields": {"cidade": "RJ"},
        }, timeout=5).json()
        assert updated["name"] == "Ana Paula"
        assert updated["customFields"]["cidade"] == "RJ"

        # templates
        tpl = httpx.post(f"{base}/api/templates", json={
            "name": "promo1", "language": "pt_BR", "category": "MARKETING", "bodyPreview": "Oi {{1}}",
        }, timeout=5).json()
        assert tpl["name"] == "promo1"

        # flows (no delay action -- keep the smoke test fast)
        flow = httpx.post(f"{base}/api/flows", json={
            "name": "Welcome", "enabled": True,
            "actions": [{"type": "add_tags", "tags": ["VIP"], "lists": [], "delaySeconds": 0}],
        }, timeout=5).json()
        assert flow["name"] == "Welcome"
        flow2 = httpx.patch(f"{base}/api/flows/{flow['id']}", json={
            "name": "Welcome v2", "enabled": True, "actions": [],
        }, timeout=5).json()
        assert flow2["name"] == "Welcome v2"

        # automations
        auto = httpx.post(f"{base}/api/automations", json={
            "name": "auto1", "enabled": True, "triggerType": "any", "triggerValue": "", "addTags": ["VIP"], "addLists": [], "items": [],
        }, timeout=5).json()
        assert auto["name"] == "auto1"
        auto2 = httpx.patch(f"{base}/api/automations/{auto['id']}", json={
            "name": "auto1 renamed", "enabled": False, "triggerType": "any", "triggerValue": "", "addTags": [], "addLists": [], "items": [],
        }, timeout=5).json()
        assert auto2["enabled"] is False

        # phone numbers
        phone = httpx.post(f"{base}/api/phone-numbers", json={
            "phoneNumberId": "PHONE_X", "displayPhoneNumber": "+551100000000", "verifiedName": "Test",
        }, timeout=5).json()
        assert phone["phoneNumberId"] == "PHONE_X"
        activated = httpx.post(f"{base}/api/phone-numbers/PHONE_X/activate", timeout=5).json()
        assert activated["active"] == "PHONE_X"

        # campaign lifecycle (mock send mode since no META_ACCESS_TOKEN)
        campaign = httpx.post(f"{base}/api/campaigns", json={
            "name": "Camp1", "templateName": "promo1", "language": "pt_BR",
            "listIds": [], "tagIds": [], "exclusionListIds": [], "phoneNumberId": "PHONE_X",
            "batchSize": 10, "batchPauseSeconds": 0, "sendNow": True,
        }, timeout=5).json()
        assert campaign["status"] == "running"

        deadline = time.time() + 15
        final_campaign = None
        while time.time() < deadline:
            rows = httpx.get(f"{base}/api/campaigns", timeout=5).json()
            final_campaign = next(c for c in rows if c["id"] == campaign["id"])
            if final_campaign["status"] == "done":
                break
            time.sleep(0.2)
        assert final_campaign["status"] == "done"

        # webhook delivery + inbound message + automation trigger
        webhook_payload = {
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "PHONE_X", "display_phone_number": "+551100000000"},
                "contacts": [{"wa_id": "5511988887777", "profile": {"name": "Ana Paula"}}],
                "messages": [{"id": "wamid.IN1", "from": "5511988887777", "type": "text", "text": {"body": "Oi"}}],
            }}]}],
        }
        resp = httpx.post(f"{base}/api/meta/webhook", json=webhook_payload, timeout=5).json()
        assert resp["messages"] == 1

        deadline = time.time() + 5
        while time.time() < deadline:
            recent = httpx.get(f"{base}/api/meta/webhooks/recent", timeout=5).json()
            if recent["count"] >= 1:
                break
            time.sleep(0.2)
        assert recent["count"] >= 1

        inbox = httpx.get(f"{base}/api/inbox", timeout=5).json()
        assert len(inbox) == 1
        convo_id = inbox[0]["id"]
        convo_detail = httpx.get(f"{base}/api/inbox/{convo_id}", timeout=5).json()
        assert convo_detail["conversation"]["unread"] == 0  # cleared on read

        # cleanup endpoints (deletes)
        del_tpl_flow = httpx.delete(f"{base}/api/flows/{flow['id']}", timeout=5).json()
        assert del_tpl_flow["deleted"] == 1
        del_auto = httpx.delete(f"{base}/api/automations/{auto['id']}", timeout=5).json()
        assert del_auto["deleted"] == 1
        del_phone = httpx.delete(f"{base}/api/phone-numbers/PHONE_X", timeout=5).json()
        assert del_phone["deleted"] == 1

        dashboard = httpx.get(f"{base}/api/dashboard", timeout=5).json()
        assert dashboard["contacts"] == 1
        assert dashboard["campaigns"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)
