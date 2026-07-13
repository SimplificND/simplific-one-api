"""
Targeted test for the trickiest part of PR B: run_flow_for_contact() was
restructured so that the STORE_MUTATION_LOCK is never held across a
FlowAction "delay" step (which can be configured up to 24h -- holding a
global write lock that long would be a severe regression, worse than the
race it fixes). This verifies the restructuring still produces the exact
same end result as before: tags/lists attached, message(s) sent, only
after all delays complete.

Updated for PR C (SQLite migration): seeding/verification goes through
server.db_run and server.db_module instead of the old server.store.
"""
import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def load_server(storage_dir: Path):
    os.environ["STORAGE_DIR"] = str(storage_dir)
    os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PHONE_NUMBER_ID", None)
    for mod in ("server", "db"):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("server")


def _create_flow_tx(conn, server, doc):
    return server.db_module.db_create_flow(conn, doc)


@pytest.mark.asyncio
async def test_flow_with_delay_applies_tags_and_sends_message_after_delay(tmp_path):
    server = load_server(tmp_path / "storage")

    now = server.now_iso()
    await server.db_run(_create_flow_tx, server, {
        "id": "flow_1",
        "name": "Delayed welcome",
        "enabled": True,
        "phoneNumberId": "PHONE_A",
        "actions": [
            {"type": "add_tags", "tags": ["novo-lead"], "lists": [], "delaySeconds": 0},
            {"type": "delay", "delaySeconds": 1, "tags": [], "lists": []},
            {"type": "send_message", "text": "Bem-vindo!", "tags": [], "lists": [], "delaySeconds": 0},
        ],
        "nodes": [], "edges": [],
        "createdAt": now, "updatedAt": now,
    })

    start = time.monotonic()
    result = await server.run_flow_for_contact("5511999998888", "flow_1", source="test")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.9, "delay action should have actually paused execution"
    assert result["messages"] == 1

    contact = await server.db_run(server.db_module.db_get_contact_by_phone, "5511999998888")
    tags = await server.db_run(server.db_module.db_list_tags, None)
    tag_names = {t["name"] for t in tags}
    assert "novo-lead" in tag_names
    tag_id = next(t["id"] for t in tags if t["name"] == "novo-lead")
    assert tag_id in contact["tags"]

    messages = await server.db_run(server.db_module.db_get_messages_for_contact, contact["id"], 50)
    out_messages = [m for m in messages if m.get("direction") == "out"]
    assert len(out_messages) == 1
    assert out_messages[0]["text"] == "Bem-vindo!"


@pytest.mark.asyncio
async def test_flow_delay_does_not_hold_global_lock(tmp_path):
    """While one flow is in its delay step, a completely unrelated write
    (e.g. another contact's tag creation) must NOT be blocked -- this is the
    whole reason the lock isn't held across delays."""
    server = load_server(tmp_path / "storage")

    now = server.now_iso()
    await server.db_run(_create_flow_tx, server, {
        "id": "flow_slow",
        "name": "Slow flow",
        "enabled": True,
        "phoneNumberId": "PHONE_A",
        "actions": [
            {"type": "delay", "delaySeconds": 2, "tags": [], "lists": []},
        ],
        "nodes": [], "edges": [],
        "createdAt": now, "updatedAt": now,
    })

    flow_task = asyncio.create_task(server.run_flow_for_contact("5511900001111", "flow_slow", source="test"))
    await asyncio.sleep(0.2)  # let the flow enter its delay

    start = time.monotonic()
    await server.create_tag(server.NamedIn(name="unrelated-tag", color="#111111", phoneNumberId=None))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"an unrelated write took {elapsed:.2f}s while a flow was mid-delay -- "
        "the store lock is being held across the delay, which would stall the whole app"
    )

    await flow_task  # let the slow flow finish so it doesn't leak into other tests
