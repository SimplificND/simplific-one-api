"""
One-time migration: reads a JSON store shaped like production's
one-api.json and loads it into a SQLite database following schema.sql.

Usage:
    python migrate_json_to_sqlite.py <input.json> <output.db>

Designed to be idempotent-safe to re-run against a fresh output path (it
creates the schema itself), and defensive against missing/malformed
optional fields -- production data has accumulated for months across many
code revisions, so fields that a newer version of server.py added may be
absent on older rows, and vice versa.
"""
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def j(value) -> str:
    """JSON-encode a value for storage in a *_json TEXT column, tolerating
    None (stored as an empty-collection-shaped default is the caller's job;
    this just needs to never blow up)."""
    return json.dumps(value if value is not None else None, ensure_ascii=False)


def migrate(input_path: Path, output_path: Path) -> dict:
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if output_path.exists():
        output_path.unlink()

    conn = sqlite3.connect(str(output_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    counts = {}

    # ---- lists / tags / custom_fields / templates / flows / phone_numbers ----
    # (inserted before contacts because contact_tags/contact_lists have FK
    # constraints referencing tags/lists)
    for row in data.get("lists", []):
        conn.execute(
            "INSERT INTO lists (id, name, phone_number_id, created_at) VALUES (?,?,?,?)",
            (row["id"], row.get("name") or "", row.get("phoneNumberId"), row.get("createdAt") or ""),
        )
    counts["lists"] = len(data.get("lists", []))

    for row in data.get("tags", []):
        conn.execute(
            "INSERT INTO tags (id, name, color, phone_number_id, created_at) VALUES (?,?,?,?,?)",
            (row["id"], row.get("name") or "", row.get("color"), row.get("phoneNumberId"), row.get("createdAt") or ""),
        )
    counts["tags"] = len(data.get("tags", []))

    for row in data.get("customFields", []):
        conn.execute(
            "INSERT INTO custom_fields (id, key, label, type, phone_number_id, created_at) VALUES (?,?,?,?,?,?)",
            (row["id"], row.get("key") or "", row.get("label"), row.get("type") or "text", row.get("phoneNumberId"), row.get("createdAt") or ""),
        )
    counts["custom_fields"] = len(data.get("customFields", []))

    for row in data.get("templates", []):
        conn.execute(
            """INSERT INTO templates
               (id, name, language, category, status, body_preview, components_json,
                phone_number_id, source, synced_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("name") or "", row.get("language") or "pt_BR", row.get("category"),
                row.get("status"), row.get("bodyPreview"), j(row.get("components") or []),
                row.get("phoneNumberId"), row.get("source"), row.get("syncedAt"), row.get("createdAt") or "",
            ),
        )
    counts["templates"] = len(data.get("templates", []))

    for row in data.get("flows", []):
        conn.execute(
            """INSERT INTO flows
               (id, name, trigger_value, enabled, actions_json, nodes_json, edges_json,
                phone_number_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("name") or "", row.get("triggerValue"),
                1 if row.get("enabled", True) else 0,
                j(row.get("actions") or []), j(row.get("nodes") or []), j(row.get("edges") or []),
                row.get("phoneNumberId"), row.get("createdAt") or "", row.get("updatedAt") or row.get("createdAt") or "",
            ),
        )
    counts["flows"] = len(data.get("flows", []))

    for row in data.get("phoneNumbers", []):
        pnid = row.get("phoneNumberId") or row.get("id")
        conn.execute(
            """INSERT INTO phone_numbers
               (id, phone_number_id, display_phone_number, verified_name, quality_rating,
                messaging_limit_tier, code_verification_status, name_status, active, source,
                registration_status, registered, registered_at,
                last_registration_response_json, last_registration_error_json,
                last_registration_error_text, last_registration_error_at,
                created_at, synced_at, refreshed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pnid, pnid, row.get("displayPhoneNumber"), row.get("verifiedName"), row.get("qualityRating"),
                row.get("messagingLimitTier"), row.get("codeVerificationStatus"), row.get("nameStatus"),
                1 if row.get("active") else 0, row.get("source"),
                row.get("registrationStatus"),
                (1 if row.get("registered") else 0) if row.get("registered") is not None else None,
                row.get("registeredAt"),
                j(row.get("lastRegistrationResponse")) if row.get("lastRegistrationResponse") is not None else None,
                j(row.get("lastRegistrationError")) if row.get("lastRegistrationError") is not None else None,
                row.get("lastRegistrationErrorText"), row.get("lastRegistrationErrorAt"),
                row.get("createdAt") or "", row.get("syncedAt"), row.get("refreshedAt"),
            ),
        )
    counts["phone_numbers"] = len(data.get("phoneNumbers", []))

    # ---- contacts ----
    seen_phones = set()
    skipped_duplicate_phone = 0
    for c in data.get("contacts", []):
        phone = c.get("phone")
        if not phone:
            continue  # cannot exist per upsert_contact(), but be defensive
        if phone in seen_phones:
            # Should not happen (upsert_contact enforces uniqueness), but if
            # production data ever had a duplicate slip in via a direct
            # write, keep the first and count the rest rather than crash
            # the whole migration.
            skipped_duplicate_phone += 1
            continue
        seen_phones.add(phone)
        conn.execute(
            """INSERT INTO contacts
               (id, phone, name, last_phone_number_id, pending_response_flow_id,
                pending_response_flows_json, pending_campaign_id, custom_fields_json,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                c["id"], phone, c.get("name"), c.get("lastPhoneNumberId"),
                c.get("pendingResponseFlowId"),
                j(c.get("pendingResponseFlows")) if c.get("pendingResponseFlows") else None,
                c.get("pendingCampaignId"),
                j(c.get("customFields") or {}),
                c.get("createdAt") or "", c.get("updatedAt") or c.get("createdAt") or "",
            ),
        )
        for pnid in dict.fromkeys(c.get("phoneNumberIds") or []):
            conn.execute(
                "INSERT OR IGNORE INTO contact_phone_numbers (contact_id, phone_number_id) VALUES (?,?)",
                (c["id"], pnid),
            )
        for tag_id in dict.fromkeys(c.get("tags") or []):
            conn.execute(
                "INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?,?)",
                (c["id"], tag_id),
            )
        for list_id in dict.fromkeys(c.get("lists") or []):
            conn.execute(
                "INSERT OR IGNORE INTO contact_lists (contact_id, list_id) VALUES (?,?)",
                (c["id"], list_id),
            )
    counts["contacts"] = len(seen_phones)
    counts["contacts_skipped_duplicate_phone"] = skipped_duplicate_phone

    # ---- automations / automation_runs ----
    for row in data.get("automations", []):
        conn.execute(
            """INSERT INTO automations
               (id, name, enabled, trigger_type, trigger_value, add_tags_json, add_lists_json,
                items_json, phone_number_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("name") or "", 1 if row.get("enabled", True) else 0,
                row.get("triggerType") or "contains", row.get("triggerValue") or "",
                j(row.get("addTags") or []), j(row.get("addLists") or []), j(row.get("items") or []),
                row.get("phoneNumberId"), row.get("createdAt") or "", row.get("updatedAt") or row.get("createdAt") or "",
            ),
        )
    counts["automations"] = len(data.get("automations", []))

    for row in data.get("automationRuns", []):
        conn.execute(
            "INSERT INTO automation_runs (id, automation_id, contact_id, phone, trigger_json, created_at) VALUES (?,?,?,?,?,?)",
            (row["id"], row.get("automationId"), row.get("contactId"), row.get("phone"), j(row.get("trigger")), row.get("createdAt") or ""),
        )
    counts["automation_runs"] = len(data.get("automationRuns", []))

    # ---- conversations ----
    for row in data.get("conversations", []):
        conn.execute(
            """INSERT INTO conversations
               (id, phone, phone_number_id, name, unread, last_message_at, last_inbound_at,
                display_phone_number, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("phone") or "", row.get("phoneNumberId"), row.get("name"),
                int(row.get("unread") or 0), row.get("lastMessageAt"), row.get("lastInboundAt"),
                row.get("displayPhoneNumber"), row.get("createdAt") or "",
            ),
        )
    counts["conversations"] = len(data.get("conversations", []))

    # ---- messages ----
    for row in data.get("messages", []):
        conn.execute(
            """INSERT INTO messages
               (id, conversation_id, contact_id, phone, phone_number_id, display_phone_number,
                direction, type, text, payload_json, status, source, provider_message_id,
                provider_response_json, error_json, error_text, delivery_status,
                delivery_payload_json, delivery_updated_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("conversationId"), row.get("contactId"), row.get("phone"),
                row.get("phoneNumberId"), row.get("displayPhoneNumber"), row.get("direction") or "out",
                row.get("type"), row.get("text"),
                j(row.get("payload")) if row.get("payload") is not None else None,
                row.get("status"), row.get("source"), row.get("providerMessageId"),
                j(row.get("providerResponse")) if row.get("providerResponse") is not None else None,
                j(row.get("error")) if row.get("error") is not None else None,
                row.get("errorText"), row.get("deliveryStatus"),
                j(row.get("deliveryPayload")) if row.get("deliveryPayload") is not None else None,
                row.get("deliveryUpdatedAt"), row.get("createdAt") or "",
            ),
        )
    counts["messages"] = len(data.get("messages", []))

    # ---- campaigns + campaign_results (+ provider id index table) ----
    total_results = 0
    for row in data.get("campaigns", []):
        conn.execute(
            """INSERT INTO campaigns
               (id, name, template_name, language, phone_number_id, response_flow_id,
                scheduled_at, target_count, sent, failed, delivered, read, button_clicks,
                last_error_json, last_error_text, config_json, status, created_at,
                started_at, last_progress_at, finished_at, failed_at, canceled_at,
                last_resume_at, last_retry_failed_at, last_status_at, last_click_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row.get("name"), row.get("templateName"), row.get("language"),
                row.get("phoneNumberId"), row.get("responseFlowId"), row.get("scheduledAt"),
                int(row.get("targetCount") or 0), int(row.get("sent") or 0), int(row.get("failed") or 0),
                int(row.get("delivered") or 0), int(row.get("read") or 0), int(row.get("buttonClicks") or 0),
                j(row.get("lastError")) if row.get("lastError") is not None else None,
                row.get("lastErrorText"), j(row.get("config") or {}), row.get("status") or "draft",
                row.get("createdAt") or "", row.get("startedAt"), row.get("lastProgressAt"),
                row.get("finishedAt"), row.get("failedAt"), row.get("canceledAt"),
                row.get("lastResumeAt"), row.get("lastRetryFailedAt"), row.get("lastStatusAt"),
                row.get("lastClickAt"), row.get("updatedAt"),
            ),
        )
        for result in row.get("results") or []:
            cur = conn.execute(
                """INSERT INTO campaign_results
                   (campaign_id, contact_id, name, phone, status, message_ids_json,
                    sent_at, delivered_at, read_at, clicked_at, button_text, error_json,
                    error_text, diagnostic_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"], result.get("contactId"), result.get("name"), result.get("phone"),
                    result.get("status"), j(result.get("messageIds") or []),
                    result.get("sentAt"), result.get("deliveredAt"), result.get("readAt"),
                    result.get("clickedAt"), result.get("buttonText"),
                    j(result.get("error")) if result.get("error") is not None else None,
                    result.get("errorText"),
                    j(result.get("diagnostic")) if result.get("diagnostic") is not None else None,
                    result.get("createdAt") or row.get("createdAt") or "",
                ),
            )
            result_row_id = cur.lastrowid
            for pmid in dict.fromkeys(result.get("providerMessageIds") or []):
                if pmid:
                    conn.execute(
                        "INSERT OR IGNORE INTO campaign_result_provider_ids (campaign_result_id, provider_message_id) VALUES (?,?)",
                        (result_row_id, pmid),
                    )
            total_results += 1
    counts["campaigns"] = len(data.get("campaigns", []))
    counts["campaign_results"] = total_results

    # ---- webhook_events ----
    for row in data.get("webhookEvents", []):
        conn.execute(
            "INSERT INTO webhook_events (id, payload_json, created_at) VALUES (?,?,?)",
            (row["id"], j(row.get("payload") or {}), row.get("createdAt") or ""),
        )
    counts["webhook_events"] = len(data.get("webhookEvents", []))

    # ---- media ----
    for row in data.get("media", []):
        conn.execute(
            "INSERT INTO media (id, filename, content_type, size, path, url, created_at) VALUES (?,?,?,?,?,?,?)",
            (row["id"], row.get("filename"), row.get("contentType"), row.get("size"), row.get("path"), row.get("url"), row.get("createdAt") or ""),
        )
    counts["media"] = len(data.get("media", []))

    # ---- settings ----
    settings = data.get("settings") or {}
    for key, value in settings.items():
        conn.execute(
            "INSERT INTO settings (key, value_json) VALUES (?,?)",
            (key, j(value)),
        )
    counts["settings_keys"] = len(settings)

    conn.commit()
    conn.close()
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python migrate_json_to_sqlite.py <input.json> <output.db>")
        sys.exit(1)
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    counts = migrate(input_path, output_path)
    print(f"Migrated {input_path} -> {output_path}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
