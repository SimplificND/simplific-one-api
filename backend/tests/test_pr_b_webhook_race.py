"""
Verification for PR B: concurrent webhook deliveries (and other concurrent
mutators) must not silently overwrite each other's updates.

Before this fix, several call sites did `data = await store.read()` ...
mutate ... `await store.write(data)` WITHOUT holding STORE_MUTATION_LOCK.
Under concurrency this is a classic lost-update race: two coroutines both
read the same base state, mutate different parts of it in memory, and
whichever writes last wins -- silently discarding the other's change.

This test fires many concurrent Meta status-update webhook deliveries, each
for a *different* campaign result row (simulating a burst of delivery
receipts arriving together during a real campaign send), and asserts every
single one of them landed. Before the fix, this reliably lost updates under
concurrency; after the fix (wrapping the read-modify-write in
STORE_MUTATION_LOCK), none are lost.

Updated for PR C (SQLite migration): the store is now backed by SQLite
instead of a JSON blob, so seeding/verification goes through server.db_run
and server.db_module instead of the old server.store/server.JsonStore.
"""
import asyncio
import importlib
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def load_server(storage_dir: Path):
    """Import a fresh copy of server.py bound to an isolated storage dir.

    We reload the module per-test (rather than relying on env vars at import
    time) so each test gets its own SQLite database / STORE_MUTATION_LOCK.
    """
    import os
    os.environ["STORAGE_DIR"] = str(storage_dir)
    os.environ.pop("META_ACCESS_TOKEN", None)
    os.environ.pop("META_PHONE_NUMBER_ID", None)
    for mod in ("server", "db"):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("server")


def webhook_status_payload(provider_message_id: str, status: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "PHONE_A", "display_phone_number": "+5511999999999"},
                    "statuses": [{
                        "id": provider_message_id,
                        "status": status,
                        "recipient_id": "5511900000000",
                    }],
                },
            }],
        }],
    }


def _seed_campaign_with_results_tx(conn, server, campaign_id: str, n: int) -> None:
    server.db_module.db_create_campaign(conn, {
        "id": campaign_id, "name": "race test", "config": {}, "status": "running",
        "createdAt": "2026-07-01T00:00:00+00:00",
    })
    for i in range(n):
        server.db_module.db_insert_campaign_result(conn, campaign_id, {
            "contactId": f"lead_{i}",
            "phone": f"5511900000{i:03d}",
            "status": "sent",
            "providerMessageIds": [f"wamid.TEST{i:04d}"],
            "sentAt": "2026-07-01T00:00:00+00:00",
            "createdAt": "2026-07-01T00:00:00+00:00",
        })


@pytest.mark.asyncio
async def test_concurrent_webhook_status_updates_all_land(tmp_path):
    server = load_server(tmp_path / "storage")

    campaign_id = "camp_race1"
    n = 60  # concurrent deliveries -- enough to reliably expose the race
    provider_ids = [f"wamid.TEST{i:04d}" for i in range(n)]

    # Seed a campaign with N result rows, each tied to a distinct provider
    # message id, all initially unacknowledged (no deliveredAt).
    await server.db_run(_seed_campaign_with_results_tx, server, campaign_id, n)

    async def fire(i: int):
        payload = webhook_status_payload(provider_ids[i], "delivered")

        class FakeRequest:
            async def json(self_inner):
                return payload

        class NullBackgroundTasks:
            def add_task(self_inner, *a, **kw):
                pass

        await server.receive_webhook(FakeRequest(), NullBackgroundTasks())

    await asyncio.gather(*(fire(i) for i in range(n)))

    campaign = await server.db_run(server.db_module.db_get_campaign, campaign_id, True)
    delivered_count = sum(1 for row in campaign["results"] if row.get("deliveredAt"))
    missing = [row["contactId"] for row in campaign["results"] if not row.get("deliveredAt")]

    assert delivered_count == n, (
        f"lost updates under concurrency: only {delivered_count}/{n} delivery statuses landed, "
        f"missing: {missing}"
    )
    # Also check no webhook events were dropped (webhook_events table
    # inserted-into concurrently by the same handler).
    event_count = await server.db_run(server.db_module.db_count_webhook_events)
    assert event_count == n


@pytest.mark.asyncio
async def test_concurrent_tag_creation_no_lost_updates(tmp_path):
    """A second, simpler concurrency check on a plain CRUD endpoint
    (create_tag) that was previously unlocked: firing many concurrent
    creates must result in exactly that many tags, none lost."""
    server = load_server(tmp_path / "storage")
    n = 40

    async def create(i: int):
        body = server.NamedIn(name=f"tag-{i}", color="#000000", phoneNumberId=None)
        return await server.create_tag(body)

    await asyncio.gather(*(create(i) for i in range(n)))

    tags = await server.db_run(server.db_module.db_list_tags, None)
    assert len(tags) == n, f"expected {n} tags, found {len(tags)} -- lost updates"


@pytest.mark.asyncio
async def test_concurrent_contact_creation_across_different_contacts(tmp_path):
    """Concurrent contact creation (different phone numbers) must not drop
    any contact due to a lost-update race on the shared store."""
    server = load_server(tmp_path / "storage")
    n = 50

    async def create(i: int):
        body = server.LeadIn(name=f"Contact {i}", phone=f"55119{i:08d}", tags=[], lists=[], customFields={}, phoneNumberId=None)
        return await server.create_contact(body)

    await asyncio.gather(*(create(i) for i in range(n)))

    contacts = await server.db_run(server.db_module.db_list_contacts, None)
    assert len(contacts) == n, f"expected {n} contacts, found {len(contacts)} -- lost updates"
