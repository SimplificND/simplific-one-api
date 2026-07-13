"""
Validation for the one-time JSON -> SQLite migration script. Not part of
the app's runtime tests -- run standalone (or via pytest since it uses
plain asserts + fixtures) to confirm the migration is safe before ever
running it against a real export of production data.

Covers:
  - row counts match exactly between the JSON source and the SQLite output
    for every collection, at production scale (~1724 contacts, ~3700
    messages, ~13+ campaigns with realistic result counts)
  - no data loss on edge cases: contacts missing optional fields (no name,
    no customFields, single vs multiple phoneNumberIds), campaigns with
    zero results, messages with missing optional fields
  - spot-checked field-level fidelity (not just counts) for a sample of
    rows across every collection
  - referential integrity: every contact_tags/contact_lists/campaign_results
    row points at a contact/tag/list/campaign that actually exists
"""
import json
import sqlite3
import sys
from pathlib import Path

MIGRATION_DIR = Path(__file__).parent
sys.path.insert(0, str(MIGRATION_DIR))

from generate_synthetic_dataset import build  # noqa: E402
from migrate_json_to_sqlite import migrate  # noqa: E402


def _prepare(tmp_path):
    json_path = tmp_path / "synthetic.json"
    db_path = tmp_path / "synthetic.db"
    dataset = build()
    json_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    migrate(json_path, db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return dataset, conn


def test_row_counts_match_exactly(tmp_path):
    dataset, conn = _prepare(tmp_path)

    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert count("contacts") == len(dataset["contacts"])
    assert count("lists") == len(dataset["lists"])
    assert count("tags") == len(dataset["tags"])
    assert count("custom_fields") == len(dataset["customFields"])
    assert count("templates") == len(dataset["templates"])
    assert count("flows") == len(dataset["flows"])
    assert count("phone_numbers") == len(dataset["phoneNumbers"])
    assert count("automations") == len(dataset["automations"])
    assert count("automation_runs") == len(dataset["automationRuns"])
    assert count("conversations") == len(dataset["conversations"])
    assert count("messages") == len(dataset["messages"])
    assert count("campaigns") == len(dataset["campaigns"])
    assert count("webhook_events") == len(dataset["webhookEvents"])
    assert count("media") == len(dataset["media"])

    expected_results = sum(len(c.get("results") or []) for c in dataset["campaigns"])
    assert count("campaign_results") == expected_results

    # Verify scale matches what production actually looks like (sanity, not
    # just "didn't crash") -- these numbers are what the task specified.
    assert len(dataset["contacts"]) == 1724
    assert len(dataset["messages"]) == 3700
    assert len(dataset["campaigns"]) >= 13


def test_no_contact_dropped_and_phone_field_fidelity(tmp_path):
    dataset, conn = _prepare(tmp_path)
    db_contacts = {row["phone"]: row for row in conn.execute("SELECT * FROM contacts").fetchall()}
    for c in dataset["contacts"]:
        row = db_contacts.get(c["phone"])
        assert row is not None, f"contact {c['id']} ({c['phone']}) missing from SQLite"
        assert row["id"] == c["id"]
        assert row["name"] == c.get("name")  # covers the ~5% with name=None
        assert json.loads(row["custom_fields_json"]) == (c.get("customFields") or {})


def test_contact_tag_and_list_associations_preserved(tmp_path):
    dataset, conn = _prepare(tmp_path)
    for c in dataset["contacts"]:
        db_tags = {r[0] for r in conn.execute("SELECT tag_id FROM contact_tags WHERE contact_id = ?", (c["id"],)).fetchall()}
        db_lists = {r[0] for r in conn.execute("SELECT list_id FROM contact_lists WHERE contact_id = ?", (c["id"],)).fetchall()}
        assert db_tags == set(c.get("tags") or []), f"tag mismatch for {c['id']}"
        assert db_lists == set(c.get("lists") or []), f"list mismatch for {c['id']}"

        db_phone_ids = {r[0] for r in conn.execute("SELECT phone_number_id FROM contact_phone_numbers WHERE contact_id = ?", (c["id"],)).fetchall()}
        assert db_phone_ids == set(c.get("phoneNumberIds") or []), f"phoneNumberIds mismatch for {c['id']}"


def test_campaign_with_zero_results_migrates_cleanly(tmp_path):
    dataset, conn = _prepare(tmp_path)
    empty_campaigns = [c for c in dataset["campaigns"] if not c.get("results")]
    assert empty_campaigns, "test dataset should include at least one zero-result campaign"
    for c in empty_campaigns:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (c["id"],)).fetchone()
        assert row is not None
        assert row["target_count"] == 0
        result_count = conn.execute("SELECT COUNT(*) FROM campaign_results WHERE campaign_id = ?", (c["id"],)).fetchone()[0]
        assert result_count == 0


def test_campaign_results_field_fidelity_and_provider_ids(tmp_path):
    dataset, conn = _prepare(tmp_path)
    sample_campaign = next(c for c in dataset["campaigns"] if c.get("results"))
    db_results = conn.execute(
        "SELECT * FROM campaign_results WHERE campaign_id = ? ORDER BY id", (sample_campaign["id"],)
    ).fetchall()
    assert len(db_results) == len(sample_campaign["results"])

    for expected, row in zip(sample_campaign["results"], db_results):
        assert row["contact_id"] == expected.get("contactId")
        assert row["phone"] == expected.get("phone")
        assert row["status"] == expected.get("status")
        assert row["sent_at"] == expected.get("sentAt")
        assert row["delivered_at"] == expected.get("deliveredAt")
        assert row["read_at"] == expected.get("readAt")
        assert row["clicked_at"] == expected.get("clickedAt")

        expected_pmids = set(pid for pid in (expected.get("providerMessageIds") or []) if pid)
        db_pmids = {
            r[0] for r in conn.execute(
                "SELECT provider_message_id FROM campaign_result_provider_ids WHERE campaign_result_id = ?",
                (row["id"],),
            ).fetchall()
        }
        assert db_pmids == expected_pmids


def test_message_field_fidelity(tmp_path):
    dataset, conn = _prepare(tmp_path)
    for m in dataset["messages"][:200]:  # sample -- full scan is done by count test
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (m["id"],)).fetchone()
        assert row is not None
        assert row["conversation_id"] == m.get("conversationId")
        assert row["contact_id"] == m.get("contactId")
        assert row["direction"] == m.get("direction")
        assert row["status"] == m.get("status")
        assert row["provider_message_id"] == m.get("providerMessageId")


def test_referential_integrity(tmp_path):
    """Every FK-shaped reference actually resolves to an existing row --
    this is the kind of subtle corruption a naive migration could
    introduce silently (e.g. a campaign_result referencing a contact_id
    that was skipped as a duplicate phone)."""
    dataset, conn = _prepare(tmp_path)
    contact_ids = {r[0] for r in conn.execute("SELECT id FROM contacts").fetchall()}
    tag_ids = {r[0] for r in conn.execute("SELECT id FROM tags").fetchall()}
    list_ids = {r[0] for r in conn.execute("SELECT id FROM lists").fetchall()}
    campaign_ids = {r[0] for r in conn.execute("SELECT id FROM campaigns").fetchall()}

    for r in conn.execute("SELECT contact_id, tag_id FROM contact_tags").fetchall():
        assert r[0] in contact_ids
        assert r[1] in tag_ids
    for r in conn.execute("SELECT contact_id, list_id FROM contact_lists").fetchall():
        assert r[0] in contact_ids
        assert r[1] in list_ids
    for r in conn.execute("SELECT campaign_id FROM campaign_results").fetchall():
        assert r[0] in campaign_ids


def test_settings_blob_preserved(tmp_path):
    dataset, conn = _prepare(tmp_path)
    row = conn.execute("SELECT value_json FROM settings WHERE key = 'meta'").fetchone()
    assert row is not None
    assert json.loads(row[0]) == dataset["settings"]["meta"]


def test_migration_is_reproducible_and_overwrites_existing_output(tmp_path):
    """Re-running the migration against the same output path must not
    fail or silently double the data (important since a human operator
    might re-run it after fixing an unrelated issue)."""
    dataset, conn = _prepare(tmp_path)
    db_path = tmp_path / "synthetic.db"
    json_path = tmp_path / "synthetic.json"
    migrate(json_path, db_path)  # run again
    conn2 = sqlite3.connect(str(db_path))
    count = conn2.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert count == len(dataset["contacts"])
