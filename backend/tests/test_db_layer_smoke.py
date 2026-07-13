"""
Standalone smoke test for backend/db.py, run BEFORE wiring it into
server.py, to catch bugs in the data-access layer cheaply and in
isolation (much easier to debug here than through 40 endpoints).
"""
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    database = db.Database(tmp_path / "test.db")
    c = database._connect()
    yield c
    c.close()


def test_contact_upsert_and_lookup(conn):
    c1 = db.db_upsert_contact(conn, "5511900000001", "Ana", "PHONE_A", new_id, now_iso)
    assert c1["phone"] == "5511900000001"
    assert c1["name"] == "Ana"
    assert c1["phoneNumberIds"] == ["PHONE_A"]

    # upserting again with same phone should not create a duplicate, and
    # should not overwrite an existing name
    c2 = db.db_upsert_contact(conn, "5511900000001", "Outro Nome", "PHONE_B", new_id, now_iso)
    assert c2["id"] == c1["id"]
    assert c2["name"] == "Ana"  # name not overwritten since it was already set
    assert set(c2["phoneNumberIds"]) == {"PHONE_A", "PHONE_B"}

    fetched = db.db_get_contact_by_phone(conn, "5511900000001")
    assert fetched["id"] == c1["id"]


def test_tag_list_dedup_and_scoping(conn):
    tag_id_1 = db.db_ensure_named_tag(conn, "VIP", "PHONE_A", new_id, now_iso)
    tag_id_2 = db.db_ensure_named_tag(conn, "vip", "PHONE_A", new_id, now_iso)  # case-insensitive dedup
    assert tag_id_1 == tag_id_2

    tag_id_other_scope = db.db_ensure_named_tag(conn, "VIP", "PHONE_B", new_id, now_iso)
    assert tag_id_other_scope != tag_id_1  # different scope = different tag

    list_id = db.db_ensure_named_list(conn, "Leads", None, new_id, now_iso)
    list_id_again = db.db_ensure_named_list(conn, "leads", None, new_id, now_iso)
    assert list_id == list_id_again


def test_attach_labels_and_contact_scoping(conn):
    contact = db.db_upsert_contact(conn, "5511900000002", "Bruno", "PHONE_A", new_id, now_iso)
    tag_id = db.db_ensure_named_tag(conn, "Quente", "PHONE_A", new_id, now_iso)
    list_id = db.db_ensure_named_list(conn, "Importados", "PHONE_A", new_id, now_iso)
    updated = db.db_attach_labels(conn, contact["id"], [tag_id], [list_id], {"cidade": "SP"}, now_iso)
    assert tag_id in updated["tags"]
    assert list_id in updated["lists"]
    assert updated["customFields"]["cidade"] == "SP"

    scoped_contacts = db.db_list_contacts(conn, "PHONE_A")
    assert any(c["id"] == contact["id"] for c in scoped_contacts)
    unscoped_other = db.db_list_contacts(conn, "PHONE_OTHER")
    assert not any(c["id"] == contact["id"] for c in unscoped_other)


def test_conversation_for_creates_and_reuses(conn):
    convo1 = db.db_conversation_for(conn, "5511900000003", "Carla", "PHONE_A", new_id, now_iso)
    convo2 = db.db_conversation_for(conn, "5511900000003", "Carla", "PHONE_A", new_id, now_iso)
    assert convo1["id"] == convo2["id"]

    # different phoneNumberId scope -> different conversation
    convo3 = db.db_conversation_for(conn, "5511900000003", "Carla", "PHONE_B", new_id, now_iso)
    assert convo3["id"] != convo1["id"]


def test_message_insert_and_conversation_read_tracking(conn):
    convo = db.db_conversation_for(conn, "5511900000004", "Duda", "PHONE_A", new_id, now_iso)
    db.db_insert_message(conn, {
        "id": new_id("msg"), "conversationId": convo["id"], "contactId": None, "phone": "5511900000004",
        "phoneNumberId": "PHONE_A", "direction": "in", "type": "text", "text": "oi", "payload": {},
        "status": "received", "createdAt": now_iso(),
    })
    db.db_update_conversation_on_message(conn, convo["id"], now_iso(), inbound=True)
    fetched = db.db_get_conversation(conn, convo["id"])
    assert fetched["unread"] == 1
    db.db_mark_conversation_read(conn, convo["id"])
    fetched2 = db.db_get_conversation(conn, convo["id"])
    assert fetched2["unread"] == 0


def test_campaign_lifecycle_and_summarize(conn):
    campaign = db.db_create_campaign(conn, {
        "id": new_id("camp"), "name": "Test", "templateName": "tpl1", "language": "pt_BR",
        "phoneNumberId": "PHONE_A", "targetCount": 2, "config": {"foo": "bar"}, "status": "running",
        "createdAt": now_iso(),
    })
    assert campaign["sent"] == 0

    contact = db.db_upsert_contact(conn, "5511900000005", "Eva", "PHONE_A", new_id, now_iso)
    row_id = db.db_insert_campaign_result(conn, campaign["id"], {
        "contactId": contact["id"], "name": "Eva", "phone": contact["phone"], "status": "sent",
        "messageIds": ["msg1"], "providerMessageIds": ["wamid.1"], "sentAt": now_iso(),
        "createdAt": now_iso(),
    })
    assert row_id
    db.db_summarize_campaign(conn, campaign["id"])
    updated = db.db_get_campaign(conn, campaign["id"])
    assert updated["sent"] == 1
    assert len(updated["results"]) == 1
    assert updated["results"][0]["providerMessageIds"] == ["wamid.1"]

    processed = db.db_campaign_processed_keys(conn, campaign["id"])
    assert ("id", contact["id"]) in processed
    assert ("phone", contact["phone"]) in processed


def test_delivery_status_updates_message_and_campaign_result(conn):
    campaign = db.db_create_campaign(conn, {
        "id": new_id("camp"), "name": "Test2", "templateName": "tpl1", "language": "pt_BR",
        "phoneNumberId": "PHONE_A", "targetCount": 1, "config": {}, "status": "running", "createdAt": now_iso(),
    })
    contact = db.db_upsert_contact(conn, "5511900000006", "Fabio", "PHONE_A", new_id, now_iso)
    convo = db.db_conversation_for(conn, contact["phone"], "Fabio", "PHONE_A", new_id, now_iso)
    msg_id = new_id("msg")
    db.db_insert_message(conn, {
        "id": msg_id, "conversationId": convo["id"], "contactId": contact["id"], "phone": contact["phone"],
        "phoneNumberId": "PHONE_A", "direction": "out", "type": "template", "text": "oi",
        "status": "sent", "providerMessageId": "wamid.99", "createdAt": now_iso(),
    })
    db.db_insert_campaign_result(conn, campaign["id"], {
        "contactId": contact["id"], "name": "Fabio", "phone": contact["phone"], "status": "sent",
        "messageIds": [msg_id], "providerMessageIds": ["wamid.99"], "sentAt": now_iso(), "createdAt": now_iso(),
    })
    db.db_summarize_campaign(conn, campaign["id"])

    affected = db.db_update_campaign_delivery_from_status(conn, {
        "providerMessageId": "wamid.99", "status": "delivered", "payload": {}, "createdAt": now_iso(),
    })
    assert affected == [campaign["id"]]

    msg = db.db_get_message(conn, msg_id)
    assert msg["deliveryStatus"] == "delivered"

    updated_campaign = db.db_get_campaign(conn, campaign["id"])
    assert updated_campaign["delivered"] == 1
    assert updated_campaign["results"][0]["deliveredAt"] is not None


def test_click_from_inbound_updates_campaign(conn):
    campaign = db.db_create_campaign(conn, {
        "id": new_id("camp"), "name": "Test3", "templateName": "tpl1", "language": "pt_BR",
        "phoneNumberId": "PHONE_A", "targetCount": 1, "config": {}, "status": "running", "createdAt": now_iso(),
    })
    contact = db.db_upsert_contact(conn, "5511900000007", "Gustavo", "PHONE_A", new_id, now_iso)
    db.db_set_contact_pending(conn, contact["id"], campaign_id=campaign["id"])
    db.db_insert_campaign_result(conn, campaign["id"], {
        "contactId": contact["id"], "name": "Gustavo", "phone": contact["phone"], "status": "sent",
        "messageIds": [], "providerMessageIds": [], "sentAt": now_iso(), "createdAt": now_iso(),
    })
    affected_campaign_id = db.db_update_campaign_click_from_inbound(conn, {
        "phone": contact["phone"], "buttonText": "Quero saber mais", "createdAt": now_iso(),
    })
    assert affected_campaign_id == campaign["id"]
    updated = db.db_get_campaign(conn, campaign["id"])
    assert updated["buttonClicks"] == 1
    assert updated["results"][0]["clickedAt"] is not None
    assert updated["results"][0]["buttonText"] == "Quero saber mais"


def test_scoped_listing_excludes_global_rows_when_scope_requested(conn):
    """Regression test for server.py's scoped() semantics: for every entity
    EXCEPT contacts (which have a phoneNumberIds list fallback), a global
    row (phoneNumberId=None) must NOT be returned when a specific scope is
    requested -- only exact phoneNumberId matches. Contacts are the one
    exception (matched via last_phone_number_id OR contact_phone_numbers).
    This was a real bug caught during the rewrite: an early version of the
    SQL used "WHERE phone_number_id = ? OR phone_number_id IS NULL", which
    would have made global tags/lists/etc visible under every scope --
    that is NOT what the original in-memory scoped() helper did.
    """
    global_tag = db.db_create_tag(conn, new_id("tag"), "Global Tag", None, None, now_iso)
    scoped_tag = db.db_create_tag(conn, new_id("tag"), "Scoped Tag", None, "PHONE_A", now_iso)

    scoped_results = db.db_list_tags(conn, "PHONE_A")
    scoped_ids = {t["id"] for t in scoped_results}
    assert scoped_ids == {scoped_tag["id"]}, "global tag leaked into scoped results"

    unscoped_results = db.db_list_tags(conn, None)
    unscoped_ids = {t["id"] for t in unscoped_results}
    assert unscoped_ids == {global_tag["id"], scoped_tag["id"]}, "unscoped listing should return everything"


def test_contacts_for_campaign_filtering(conn):
    tag_vip = db.db_ensure_named_tag(conn, "VIP", None, new_id, now_iso)
    tag_frio = db.db_ensure_named_tag(conn, "Frio", None, new_id, now_iso)
    list_a = db.db_ensure_named_list(conn, "Lista A", None, new_id, now_iso)
    blacklist = db.db_ensure_named_list(conn, "Blacklist", None, new_id, now_iso)

    c1 = db.db_upsert_contact(conn, "5511900000008", "H1", None, new_id, now_iso)
    db.db_attach_labels(conn, c1["id"], [tag_vip], [list_a], None, now_iso)
    c2 = db.db_upsert_contact(conn, "5511900000009", "H2", None, new_id, now_iso)
    db.db_attach_labels(conn, c2["id"], [tag_frio], [list_a], None, now_iso)
    c3 = db.db_upsert_contact(conn, "5511900000010", "H3", None, new_id, now_iso)
    db.db_attach_labels(conn, c3["id"], [tag_vip], [list_a, blacklist], None, now_iso)

    selected = db.db_contacts_for_campaign(conn, None, [list_a], [tag_vip], [blacklist])
    selected_ids = {c["id"] for c in selected}
    assert selected_ids == {c1["id"]}  # c2 wrong tag, c3 excluded via blacklist
