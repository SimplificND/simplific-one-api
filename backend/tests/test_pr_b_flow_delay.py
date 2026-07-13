"""
Targeted test for the trickiest part of PR B: run_flow_for_contact() was
restructured so that the STORE_MUTATION_LOCK is never held across a
FlowAction "delay" step (which can be configured up to 24h -- holding a
global write lock that long would be a severe regression, worse than the
race it fixes). This verifies the restructuring still produces the exact
same end result as before: tags/lists attached, message(s) sent, only
after all delays complete.
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
    if "server" in sys.modules:
        del sys.modules["server"]
    return importlib.import_module("server")


@pytest.mark.asyncio
async def test_flow_with_delay_applies_tags_and_sends_message_after_delay(tmp_path):
    server = load_server(tmp_path / "storage")

    data = server.JsonStore.empty()
    data["flows"].append({
        "id": "flow_1",
        "name": "Delayed welcome",
        "enabled": True,
        "phoneNumberId": "PHONE_A",
        "actions": [
            {"type": "add_tags", "tags": ["novo-lead"], "lists": [], "delaySeconds": 0},
            {"type": "delay", "delaySeconds": 1, "tags": [], "lists": []},
            {"type": "send_message", "text": "Bem-vindo!", "tags": [], "lists": [], "delaySeconds": 0},
        ],
    })
    await server.store.write(data)

    start = time.monotonic()
    result = await server.run_flow_for_contact("5511999998888", "flow_1", source="test")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.9, "delay action should have actually paused execution"
    assert result["messages"] == 1

    final = await server.store.read()
    contact = next(c for c in final["contacts"] if c["phone"] == "5511999998888")
    assert "novo-lead" in [t["name"] if isinstance(t, dict) else t for t in []] or True  # tags stored as ids; check via resolution below
    tag_names = {t["name"] for t in final["tags"]}
    assert "novo-lead" in tag_names
    tag_id = next(t["id"] for t in final["tags"] if t["name"] == "novo-lead")
    assert tag_id in contact["tags"]

    out_messages = [m for m in final["messages"] if m.get("contactId") == contact["id"] and m.get("direction") == "out"]
    assert len(out_messages) == 1
    assert out_messages[0]["text"] == "Bem-vindo!"


@pytest.mark.asyncio
async def test_flow_delay_does_not_hold_global_lock(tmp_path):
    """While one flow is in its delay step, a completely unrelated write
    (e.g. another contact's tag creation) must NOT be blocked -- this is the
    whole reason the lock isn't held across delays."""
    server = load_server(tmp_path / "storage")

    data = server.JsonStore.empty()
    data["flows"].append({
        "id": "flow_slow",
        "name": "Slow flow",
        "enabled": True,
        "phoneNumberId": "PHONE_A",
        "actions": [
            {"type": "delay", "delaySeconds": 2, "tags": [], "lists": []},
        ],
    })
    await server.store.write(data)

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
