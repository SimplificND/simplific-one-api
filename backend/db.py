"""
SQLite-backed data access layer, replacing the single-JSON-blob JsonStore.

Design principles (matching PR#1's own precedent of offloading blocking I/O
via asyncio.to_thread, rather than adding a new async DB dependency):
  - stdlib `sqlite3` only, no aiosqlite -- every blocking call is wrapped in
    `asyncio.to_thread`.
  - WAL mode + a fresh connection per call (SQLite connections are cheap to
    open; WAL allows genuinely concurrent readers while one writer holds a
    transaction). This is what actually fixes the reported production
    slowness: /api/settings, /api/inbox, /api/campaigns were each paying the
    cost of reading + json.loads()-ing the ENTIRE one-api.json file (which
    only grows) just to serve a handful of rows. Every function below only
    touches the rows it actually needs, via indexed queries.
  - Every function returns/accepts the SAME dict shapes the old JSON-blob
    code used (e.g. a contact dict has "tags"/"lists"/"phoneNumberIds"/
    "customFields" keys), so the endpoint logic in server.py -- scoped(),
    ensure_named_tag(), attach_labels(), summarize_campaign(), etc os --
    keeps behaving exactly the same from the caller's point of view.
  - Write-side concurrency control (STORE_MUTATION_LOCK) stays in server.py
    exactly as PR B left it; this module does not change that model, it
    only changes what runs underneath it (targeted SQL instead of a full
    file rewrite).
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

SCHEMA_PATH = Path(__file__).parent / "migration" / "schema.sql"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # NORMAL (rather than the default FULL) is the standard pairing with
        # WAL mode: it skips an fsync on every single commit (each one of
        # our short-lived write transactions was paying that cost), relying
        # on the WAL file for crash consistency. This only weakens
        # durability against an OS-level crash/power loss (not an app
        # crash/restart, which PR A's auto-resume already covers) -- a
        # standard, well-documented trade-off for WAL-mode SQLite under
        # concurrent write load.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()


def _loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def _contact_row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    contact_id = row["id"]
    tags = [r[0] for r in conn.execute("SELECT tag_id FROM contact_tags WHERE contact_id = ?", (contact_id,)).fetchall()]
    lists = [r[0] for r in conn.execute("SELECT list_id FROM contact_lists WHERE contact_id = ?", (contact_id,)).fetchall()]
    phone_number_ids = [r[0] for r in conn.execute("SELECT phone_number_id FROM contact_phone_numbers WHERE contact_id = ?", (contact_id,)).fetchall()]
    doc = {
        "id": contact_id,
        "phone": row["phone"],
        "name": row["name"],
        "lastPhoneNumberId": row["last_phone_number_id"],
        "phoneNumberIds": sorted(phone_number_ids),
        "tags": sorted(tags),
        "lists": sorted(lists),
        "customFields": _loads(row["custom_fields_json"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    pending_flow_id = row["pending_response_flow_id"]
    pending_flows_map = _loads(row["pending_response_flows_json"])
    pending_campaign_id = row["pending_campaign_id"]
    if pending_flow_id:
        doc["pendingResponseFlowId"] = pending_flow_id
    if pending_flows_map:
        doc["pendingResponseFlows"] = pending_flows_map
    if pending_campaign_id:
        doc["pendingCampaignId"] = pending_campaign_id
    return doc


def db_get_contact_by_phone(conn: sqlite3.Connection, phone: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM contacts WHERE phone = ?", (phone,)).fetchone()
    return _contact_row_to_dict(conn, row) if row else None


def db_get_contact_by_id(conn: sqlite3.Connection, contact_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return _contact_row_to_dict(conn, row) if row else None


def db_list_contacts(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM contacts ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT c.* FROM contacts c
               LEFT JOIN contact_phone_numbers cpn ON cpn.contact_id = c.id
               WHERE c.last_phone_number_id = ? OR cpn.phone_number_id = ?
               ORDER BY c.created_at DESC""",
            (scope, scope),
        ).fetchall()
    return [_contact_row_to_dict(conn, row) for row in rows]


def db_upsert_contact(
    conn: sqlite3.Connection,
    phone: str,
    name: Optional[str],
    phone_number_id: Optional[str],
    new_id_fn,
    now_iso_fn,
) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM contacts WHERE phone = ?", (phone,)).fetchone()
    now = now_iso_fn()
    if existing:
        contact_id = existing["id"]
        set_clauses = ["updated_at = ?"]
        params: list[Any] = [now]
        if name and not existing["name"]:
            set_clauses.append("name = ?")
            params.append(name)
        if phone_number_id:
            set_clauses.append("last_phone_number_id = ?")
            params.append(phone_number_id)
        params.append(contact_id)
        conn.execute(f"UPDATE contacts SET {', '.join(set_clauses)} WHERE id = ?", params)
        if phone_number_id:
            conn.execute(
                "INSERT OR IGNORE INTO contact_phone_numbers (contact_id, phone_number_id) VALUES (?,?)",
                (contact_id, phone_number_id),
            )
        return db_get_contact_by_id(conn, contact_id)

    contact_id = new_id_fn("lead")
    conn.execute(
        """INSERT INTO contacts (id, phone, name, last_phone_number_id, custom_fields_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (contact_id, phone, name, phone_number_id, "{}", now, now),
    )
    if phone_number_id:
        conn.execute(
            "INSERT OR IGNORE INTO contact_phone_numbers (contact_id, phone_number_id) VALUES (?,?)",
            (contact_id, phone_number_id),
        )
    return db_get_contact_by_id(conn, contact_id)


def db_mark_contact_scope(conn: sqlite3.Connection, contact_id: str, phone_number_id: Optional[str], now_iso_fn) -> None:
    scope = (phone_number_id or "").strip()
    if not scope:
        return
    conn.execute(
        "INSERT OR IGNORE INTO contact_phone_numbers (contact_id, phone_number_id) VALUES (?,?)",
        (contact_id, scope),
    )
    conn.execute(
        "UPDATE contacts SET last_phone_number_id = ?, updated_at = ? WHERE id = ?",
        (scope, now_iso_fn(), contact_id),
    )


def db_update_contact_full(
    conn: sqlite3.Connection,
    contact_id: str,
    name: Optional[str],
    tag_ids: list[str],
    list_ids: list[str],
    custom_fields: dict[str, Any],
    phone_number_id: Optional[str],
    now_iso_fn,
) -> Optional[dict[str, Any]]:
    existing = conn.execute("SELECT id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not existing:
        return None
    conn.execute(
        "UPDATE contacts SET name = ?, custom_fields_json = ?, updated_at = ? WHERE id = ?",
        (name, _dumps(custom_fields or {}), now_iso_fn(), contact_id),
    )
    conn.execute("DELETE FROM contact_tags WHERE contact_id = ?", (contact_id,))
    for tag_id in dict.fromkeys(tag_ids):
        conn.execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?,?)", (contact_id, tag_id))
    conn.execute("DELETE FROM contact_lists WHERE contact_id = ?", (contact_id,))
    for list_id in dict.fromkeys(list_ids):
        conn.execute("INSERT OR IGNORE INTO contact_lists (contact_id, list_id) VALUES (?,?)", (contact_id, list_id))
    db_mark_contact_scope(conn, contact_id, phone_number_id, now_iso_fn)
    return db_get_contact_by_id(conn, contact_id)


def db_attach_labels(
    conn: sqlite3.Connection,
    contact_id: str,
    tag_ids: list[str],
    list_ids: list[str],
    custom_fields: Optional[dict[str, Any]],
    now_iso_fn,
) -> dict[str, Any]:
    for tag_id in dict.fromkeys(t for t in (tag_ids or []) if t):
        conn.execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?,?)", (contact_id, tag_id))
    for list_id in dict.fromkeys(l for l in (list_ids or []) if l):
        conn.execute("INSERT OR IGNORE INTO contact_lists (contact_id, list_id) VALUES (?,?)", (contact_id, list_id))
    if custom_fields:
        row = conn.execute("SELECT custom_fields_json FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        current = _loads(row["custom_fields_json"], {}) if row else {}
        current.update(custom_fields)
        conn.execute("UPDATE contacts SET custom_fields_json = ? WHERE id = ?", (_dumps(current), contact_id))
    conn.execute("UPDATE contacts SET updated_at = ? WHERE id = ?", (now_iso_fn(), contact_id))
    return db_get_contact_by_id(conn, contact_id)


def db_set_contact_pending(
    conn: sqlite3.Connection,
    contact_id: str,
    response_flow_id: Optional[str] = None,
    button_flow_map: Optional[dict[str, str]] = None,
    campaign_id: Optional[str] = None,
) -> None:
    set_clauses = []
    params: list[Any] = []
    if button_flow_map:
        set_clauses.append("pending_response_flows_json = ?")
        params.append(_dumps(button_flow_map))
    elif response_flow_id:
        set_clauses.append("pending_response_flow_id = ?")
        params.append(response_flow_id)
    if campaign_id:
        set_clauses.append("pending_campaign_id = ?")
        params.append(campaign_id)
    if not set_clauses:
        return
    params.append(contact_id)
    conn.execute(f"UPDATE contacts SET {', '.join(set_clauses)} WHERE id = ?", params)


def db_clear_contact_pending_flow(conn: sqlite3.Connection, contact_id: str) -> None:
    conn.execute(
        "UPDATE contacts SET pending_response_flow_id = NULL, pending_response_flows_json = NULL WHERE id = ?",
        (contact_id,),
    )


# ---------------------------------------------------------------------------
# Lists / Tags / Custom fields
# ---------------------------------------------------------------------------

def db_list_lists(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM lists").fetchall()
    else:
        rows = conn.execute("SELECT * FROM lists WHERE phone_number_id = ?", (scope,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "phoneNumberId": r["phone_number_id"], "createdAt": r["created_at"]} for r in rows]


def db_create_list(conn: sqlite3.Connection, list_id: str, name: str, phone_number_id: Optional[str], now_iso_fn) -> dict[str, Any]:
    now = now_iso_fn()
    conn.execute("INSERT INTO lists (id, name, phone_number_id, created_at) VALUES (?,?,?,?)", (list_id, name, phone_number_id, now))
    return {"id": list_id, "name": name, "phoneNumberId": phone_number_id, "createdAt": now}


def db_ensure_named_list(conn: sqlite3.Connection, name: str, phone_number_id: Optional[str], new_id_fn, now_iso_fn) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    scope = (phone_number_id or "").strip()
    row = conn.execute(
        "SELECT id FROM lists WHERE lower(name) = lower(?) AND COALESCE(phone_number_id, '') = ?",
        (clean, scope),
    ).fetchone()
    if row:
        return row["id"]
    list_id = new_id_fn("list")
    conn.execute(
        "INSERT INTO lists (id, name, phone_number_id, created_at) VALUES (?,?,?,?)",
        (list_id, clean, scope or None, now_iso_fn()),
    )
    return list_id


def db_list_tags(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM tags").fetchall()
    else:
        rows = conn.execute("SELECT * FROM tags WHERE phone_number_id = ?", (scope,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "color": r["color"], "phoneNumberId": r["phone_number_id"], "createdAt": r["created_at"]} for r in rows]


def db_create_tag(conn: sqlite3.Connection, tag_id: str, name: str, color: Optional[str], phone_number_id: Optional[str], now_iso_fn) -> dict[str, Any]:
    now = now_iso_fn()
    color = color or "#84ff00"
    conn.execute("INSERT INTO tags (id, name, color, phone_number_id, created_at) VALUES (?,?,?,?,?)", (tag_id, name, color, phone_number_id, now))
    return {"id": tag_id, "name": name, "color": color, "phoneNumberId": phone_number_id, "createdAt": now}


def db_ensure_named_tag(conn: sqlite3.Connection, name: str, phone_number_id: Optional[str], new_id_fn, now_iso_fn) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    scope = (phone_number_id or "").strip()
    row = conn.execute(
        "SELECT id FROM tags WHERE lower(name) = lower(?) AND COALESCE(phone_number_id, '') = ?",
        (clean, scope),
    ).fetchone()
    if row:
        return row["id"]
    tag_id = new_id_fn("tag")
    conn.execute(
        "INSERT INTO tags (id, name, color, phone_number_id, created_at) VALUES (?,?,?,?,?)",
        (tag_id, clean, "#84ff00", scope or None, now_iso_fn()),
    )
    return tag_id


def db_list_custom_fields(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM custom_fields").fetchall()
    else:
        rows = conn.execute("SELECT * FROM custom_fields WHERE phone_number_id = ?", (scope,)).fetchall()
    return [{"id": r["id"], "key": r["key"], "label": r["label"], "type": r["type"], "phoneNumberId": r["phone_number_id"], "createdAt": r["created_at"]} for r in rows]


def db_upsert_custom_field(conn: sqlite3.Connection, field_id: str, key: str, label: str, type_: str, phone_number_id: Optional[str], now_iso_fn) -> dict[str, Any]:
    now = now_iso_fn()
    existing = conn.execute("SELECT id FROM custom_fields WHERE key = ? AND COALESCE(phone_number_id, '') = ?", (key, phone_number_id or "")).fetchone()
    if existing:
        conn.execute("UPDATE custom_fields SET id=?, label=?, type=?, phone_number_id=? WHERE key = ? AND COALESCE(phone_number_id,'') = ?",
                     (field_id, label, type_, phone_number_id, key, phone_number_id or ""))
    else:
        conn.execute("INSERT INTO custom_fields (id, key, label, type, phone_number_id, created_at) VALUES (?,?,?,?,?,?)",
                     (field_id, key, label, type_, phone_number_id, now))
    return {"id": field_id, "key": key, "label": label, "type": type_, "phoneNumberId": phone_number_id, "createdAt": now}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _template_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "language": r["language"], "category": r["category"],
        "status": r["status"], "bodyPreview": r["body_preview"], "components": _loads(r["components_json"], []),
        "phoneNumberId": r["phone_number_id"], "source": r["source"], "syncedAt": r["synced_at"],
        "createdAt": r["created_at"],
    }


def db_list_templates(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM templates").fetchall()
    else:
        rows = conn.execute("SELECT * FROM templates WHERE phone_number_id = ?", (scope,)).fetchall()
    return [_template_row_to_dict(r) for r in rows]


def db_get_template(conn: sqlite3.Connection, template_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return _template_row_to_dict(row) if row else None


def db_template_by_name(conn: sqlite3.Connection, name: str, language: str, phone_number_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    if language:
        rows = conn.execute("SELECT * FROM templates WHERE name = ? AND language = ?", (name, language)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM templates WHERE name = ?", (name,)).fetchall()
    matches = [_template_row_to_dict(r) for r in rows]
    if phone_number_id:
        scoped_match = next((t for t in matches if (t.get("phoneNumberId") or "") == phone_number_id), None)
        if scoped_match:
            return scoped_match
    return next(iter(matches), None)


def db_create_template(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO templates (id, name, language, category, status, body_preview, components_json, phone_number_id, source, synced_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (doc["id"], doc.get("name"), doc.get("language") or "pt_BR", doc.get("category"), doc.get("status"),
         doc.get("bodyPreview"), _dumps(doc.get("components") or []), doc.get("phoneNumberId"), doc.get("source"),
         doc.get("syncedAt"), doc["createdAt"]),
    )
    return doc


def db_upsert_synced_template(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    existing = conn.execute("SELECT id FROM templates WHERE id = ?", (doc["id"],)).fetchone()
    if existing:
        conn.execute(
            """UPDATE templates SET name=?, language=?, category=?, status=?, body_preview=?, components_json=?,
               phone_number_id=?, source=?, synced_at=? WHERE id=?""",
            (doc.get("name"), doc.get("language"), doc.get("category"), doc.get("status"), doc.get("bodyPreview"),
             _dumps(doc.get("components") or []), doc.get("phoneNumberId"), doc.get("source"), doc.get("syncedAt"), doc["id"]),
        )
    else:
        db_create_template(conn, doc)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

def _flow_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "triggerValue": r["trigger_value"], "enabled": bool(r["enabled"]),
        "actions": _loads(r["actions_json"], []), "nodes": _loads(r["nodes_json"], []), "edges": _loads(r["edges_json"], []),
        "phoneNumberId": r["phone_number_id"], "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def db_list_flows(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM flows").fetchall()
    else:
        rows = conn.execute("SELECT * FROM flows WHERE phone_number_id = ?", (scope,)).fetchall()
    return [_flow_row_to_dict(r) for r in rows]


def db_get_flow(conn: sqlite3.Connection, flow_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
    return _flow_row_to_dict(row) if row else None


def db_create_flow(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO flows (id, name, trigger_value, enabled, actions_json, nodes_json, edges_json, phone_number_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (doc["id"], doc.get("name"), doc.get("triggerValue"), 1 if doc.get("enabled", True) else 0,
         _dumps(doc.get("actions") or []), _dumps(doc.get("nodes") or []), _dumps(doc.get("edges") or []),
         doc.get("phoneNumberId"), doc["createdAt"], doc["updatedAt"]),
    )
    return doc


def db_update_flow(conn: sqlite3.Connection, flow_id: str, doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    existing = conn.execute("SELECT id FROM flows WHERE id = ?", (flow_id,)).fetchone()
    if not existing:
        return None
    conn.execute(
        """UPDATE flows SET name=?, trigger_value=?, enabled=?, actions_json=?, nodes_json=?, edges_json=?,
           phone_number_id=?, updated_at=? WHERE id=?""",
        (doc.get("name"), doc.get("triggerValue"), 1 if doc.get("enabled", True) else 0,
         _dumps(doc.get("actions") or []), _dumps(doc.get("nodes") or []), _dumps(doc.get("edges") or []),
         doc.get("phoneNumberId"), doc["updatedAt"], flow_id),
    )
    return db_get_flow(conn, flow_id)


def db_delete_flow(conn: sqlite3.Connection, flow_id: str) -> int:
    cur = conn.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
    return cur.rowcount


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------

def _phone_number_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    """codeVerificationStatus/nameStatus/syncedAt/refreshedAt are only set
    by the Meta-sync/refresh code paths, never by manual add -- omitted
    when NULL to match the original JSON shape."""
    doc = {
        "id": r["id"], "phoneNumberId": r["phone_number_id"], "displayPhoneNumber": r["display_phone_number"],
        "verifiedName": r["verified_name"], "qualityRating": r["quality_rating"],
        "messagingLimitTier": r["messaging_limit_tier"],
        "active": bool(r["active"]), "source": r["source"], "createdAt": r["created_at"],
    }
    if r["code_verification_status"] is not None:
        doc["codeVerificationStatus"] = r["code_verification_status"]
    if r["name_status"] is not None:
        doc["nameStatus"] = r["name_status"]
    if r["synced_at"] is not None:
        doc["syncedAt"] = r["synced_at"]
    if r["refreshed_at"] is not None:
        doc["refreshedAt"] = r["refreshed_at"]
    if r["registration_status"] is not None:
        doc["registrationStatus"] = r["registration_status"]
    if r["registered"] is not None:
        doc["registered"] = bool(r["registered"])
    if r["registered_at"] is not None:
        doc["registeredAt"] = r["registered_at"]
    if r["last_registration_response_json"] is not None:
        doc["lastRegistrationResponse"] = _loads(r["last_registration_response_json"])
    if r["last_registration_error_json"] is not None:
        doc["lastRegistrationError"] = _loads(r["last_registration_error_json"])
    if r["last_registration_error_text"] is not None:
        doc["lastRegistrationErrorText"] = r["last_registration_error_text"]
    if r["last_registration_error_at"] is not None:
        doc["lastRegistrationErrorAt"] = r["last_registration_error_at"]
    return doc


def db_list_phone_numbers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM phone_numbers").fetchall()
    return [_phone_number_row_to_dict(r) for r in rows]


def db_get_phone_number(conn: sqlite3.Connection, phone_number_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM phone_numbers WHERE phone_number_id = ?", (phone_number_id,)).fetchone()
    return _phone_number_row_to_dict(row) if row else None


def db_upsert_phone_number(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    pnid = doc.get("phoneNumberId") or doc.get("id")
    existing = conn.execute("SELECT id FROM phone_numbers WHERE id = ?", (pnid,)).fetchone()
    shared_fields = {
        "phone_number_id": pnid,
        "display_phone_number": doc.get("displayPhoneNumber"),
        "verified_name": doc.get("verifiedName"),
        "quality_rating": doc.get("qualityRating"),
        "messaging_limit_tier": doc.get("messagingLimitTier"),
        "code_verification_status": doc.get("codeVerificationStatus"),
        "name_status": doc.get("nameStatus"),
        "active": 1 if doc.get("active") else 0,
        "source": doc.get("source"),
        "registration_status": doc.get("registrationStatus"),
        "registered": (1 if doc.get("registered") else 0) if doc.get("registered") is not None else None,
        "registered_at": doc.get("registeredAt"),
        "last_registration_response_json": _dumps(doc["lastRegistrationResponse"]) if doc.get("lastRegistrationResponse") is not None else None,
        "last_registration_error_json": _dumps(doc["lastRegistrationError"]) if doc.get("lastRegistrationError") is not None else None,
        "last_registration_error_text": doc.get("lastRegistrationErrorText"),
        "last_registration_error_at": doc.get("lastRegistrationErrorAt"),
        "synced_at": doc.get("syncedAt"),
        "refreshed_at": doc.get("refreshedAt"),
    }
    if existing:
        set_clause = ", ".join(f"{col} = ?" for col in shared_fields)
        conn.execute(
            f"UPDATE phone_numbers SET {set_clause} WHERE id = ?",
            list(shared_fields.values()) + [pnid],
        )
    else:
        all_fields = {"id": pnid, **shared_fields, "created_at": doc.get("createdAt") or ""}
        columns = ", ".join(all_fields.keys())
        placeholders = ", ".join("?" for _ in all_fields)
        conn.execute(
            f"INSERT INTO phone_numbers ({columns}) VALUES ({placeholders})",
            list(all_fields.values()),
        )
    return db_get_phone_number(conn, pnid)


def db_activate_phone_number(conn: sqlite3.Connection, phone_number_id: str) -> bool:
    row = conn.execute("SELECT id FROM phone_numbers WHERE phone_number_id = ? OR id = ?", (phone_number_id, phone_number_id)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE phone_numbers SET active = 0")
    conn.execute("UPDATE phone_numbers SET active = 1 WHERE phone_number_id = ? OR id = ?", (phone_number_id, phone_number_id))
    return True


def db_delete_phone_number(conn: sqlite3.Connection, phone_number_id: str) -> int:
    cur = conn.execute("DELETE FROM phone_numbers WHERE phone_number_id = ? OR id = ?", (phone_number_id, phone_number_id))
    return cur.rowcount


# ---------------------------------------------------------------------------
# Automations / automation runs
# ---------------------------------------------------------------------------

def _automation_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]), "triggerType": r["trigger_type"],
        "triggerValue": r["trigger_value"], "addTags": _loads(r["add_tags_json"], []),
        "addLists": _loads(r["add_lists_json"], []), "items": _loads(r["items_json"], []),
        "phoneNumberId": r["phone_number_id"], "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def db_list_automations(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM automations").fetchall()
    else:
        rows = conn.execute("SELECT * FROM automations WHERE phone_number_id = ?", (scope,)).fetchall()
    return [_automation_row_to_dict(r) for r in rows]


def db_create_automation(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO automations (id, name, enabled, trigger_type, trigger_value, add_tags_json, add_lists_json,
           items_json, phone_number_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (doc["id"], doc.get("name"), 1 if doc.get("enabled", True) else 0, doc.get("triggerType") or "contains",
         doc.get("triggerValue") or "", _dumps(doc.get("addTags") or []), _dumps(doc.get("addLists") or []),
         _dumps(doc.get("items") or []), doc.get("phoneNumberId"), doc["createdAt"], doc["updatedAt"]),
    )
    return doc


def db_update_automation(conn: sqlite3.Connection, automation_id: str, doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    existing = conn.execute("SELECT id FROM automations WHERE id = ?", (automation_id,)).fetchone()
    if not existing:
        return None
    conn.execute(
        """UPDATE automations SET name=?, enabled=?, trigger_type=?, trigger_value=?, add_tags_json=?,
           add_lists_json=?, items_json=?, phone_number_id=?, updated_at=? WHERE id=?""",
        (doc.get("name"), 1 if doc.get("enabled", True) else 0, doc.get("triggerType") or "contains",
         doc.get("triggerValue") or "", _dumps(doc.get("addTags") or []), _dumps(doc.get("addLists") or []),
         _dumps(doc.get("items") or []), doc.get("phoneNumberId"), doc["updatedAt"], automation_id),
    )
    row = conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
    return _automation_row_to_dict(row)


def db_delete_automation(conn: sqlite3.Connection, automation_id: str) -> int:
    cur = conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
    return cur.rowcount


def db_list_automations_matching_scope(conn: sqlite3.Connection, phone_number_id: Optional[str]) -> list[dict[str, Any]]:
    return db_list_automations(conn, phone_number_id)


def db_insert_automation_run(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO automation_runs (id, automation_id, contact_id, phone, trigger_json, created_at) VALUES (?,?,?,?,?,?)",
        (doc["id"], doc.get("automationId"), doc.get("contactId"), doc.get("phone"), _dumps(doc.get("trigger")), doc["createdAt"]),
    )


def db_count_automation_runs(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> int:
    if not phone_number_id:
        return conn.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0]
    row = conn.execute(
        """SELECT COUNT(*) FROM automation_runs ar
           JOIN automations a ON a.id = ar.automation_id
           WHERE a.phone_number_id = ? OR a.phone_number_id IS NULL""",
        (phone_number_id,),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def _conversation_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    """lastInboundAt/displayPhoneNumber are only ever set by the webhook
    handler (an inbound message), never at conversation creation -- omitted
    when NULL to match the original JSON shape."""
    doc = {
        "id": r["id"], "phone": r["phone"], "phoneNumberId": r["phone_number_id"], "name": r["name"],
        "unread": r["unread"], "lastMessageAt": r["last_message_at"], "createdAt": r["created_at"],
    }
    if r["last_inbound_at"] is not None:
        doc["lastInboundAt"] = r["last_inbound_at"]
    if r["display_phone_number"] is not None:
        doc["displayPhoneNumber"] = r["display_phone_number"]
    return doc


def db_conversation_for(
    conn: sqlite3.Connection, phone: str, name: Optional[str], phone_number_id: Optional[str], new_id_fn, now_iso_fn
) -> dict[str, Any]:
    scope = phone_number_id or ""
    row = conn.execute(
        "SELECT * FROM conversations WHERE phone = ? AND COALESCE(phone_number_id, '') = ?", (phone, scope)
    ).fetchone()
    if not row and not phone_number_id:
        row = conn.execute("SELECT * FROM conversations WHERE phone = ?", (phone,)).fetchone()
    if row:
        if phone_number_id and not row["phone_number_id"]:
            conn.execute("UPDATE conversations SET phone_number_id = ? WHERE id = ?", (phone_number_id, row["id"]))
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone()
        return _conversation_row_to_dict(row)
    convo_id = new_id_fn("conv")
    now = now_iso_fn()
    conn.execute(
        """INSERT INTO conversations (id, phone, phone_number_id, name, unread, last_message_at, created_at)
           VALUES (?,?,?,?,0,?,?)""",
        (convo_id, phone, phone_number_id, name, now, now),
    )
    return _conversation_row_to_dict(conn.execute("SELECT * FROM conversations WHERE id = ?", (convo_id,)).fetchone())


def db_get_conversation(conn: sqlite3.Connection, conversation_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return _conversation_row_to_dict(row) if row else None


def db_list_conversations(conn: sqlite3.Connection, phone_number_id: Optional[str] = None) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM conversations ORDER BY last_message_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE phone_number_id = ? ORDER BY last_message_at DESC",
            (scope,),
        ).fetchall()
    return [_conversation_row_to_dict(r) for r in rows]


def db_update_conversation_on_message(conn: sqlite3.Connection, conversation_id: str, last_message_at: str,
                                       phone_number_id: Optional[str] = None, display_phone_number: Optional[str] = None,
                                       inbound: bool = False) -> None:
    if inbound:
        conn.execute(
            "UPDATE conversations SET last_message_at = ?, last_inbound_at = ?, unread = unread + 1 WHERE id = ?",
            (last_message_at, last_message_at, conversation_id),
        )
    else:
        conn.execute("UPDATE conversations SET last_message_at = ? WHERE id = ?", (last_message_at, conversation_id))
    if phone_number_id:
        conn.execute("UPDATE conversations SET phone_number_id = ?, display_phone_number = ? WHERE id = ?",
                     (phone_number_id, display_phone_number, conversation_id))


def db_mark_conversation_read(conn: sqlite3.Connection, conversation_id: str) -> None:
    conn.execute("UPDATE conversations SET unread = 0 WHERE id = ?", (conversation_id,))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _message_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    """Matches the two message-creation code paths in server.py exactly:
      - outbound (send_sequence / persist_campaign_batch) always sets
        source/providerMessageId/providerResponse/error/errorText, even
        when the value is None -- so those keys are always present here
        for direction == 'out'.
      - inbound (the webhook handler) always sets displayPhoneNumber (even
        when None) and NEVER sets source/providerMessageId/providerResponse/
        error/errorText at all -- so those keys are omitted here for
        direction == 'in'.
    deliveryStatus/deliveryPayload/deliveryUpdatedAt are only ever set later
    by a delivery-status webhook updating an outbound message -- omitted
    when NULL regardless of direction.
    """
    doc = {
        "id": r["id"], "conversationId": r["conversation_id"], "contactId": r["contact_id"], "phone": r["phone"],
        "phoneNumberId": r["phone_number_id"], "direction": r["direction"], "type": r["type"], "text": r["text"],
        "payload": _loads(r["payload_json"], {}), "status": r["status"], "createdAt": r["created_at"],
    }
    if r["direction"] == "out":
        doc["source"] = r["source"]
        doc["providerMessageId"] = r["provider_message_id"]
        doc["providerResponse"] = _loads(r["provider_response_json"])
        doc["error"] = _loads(r["error_json"])
        doc["errorText"] = r["error_text"]
    else:
        doc["displayPhoneNumber"] = r["display_phone_number"]
    for key, value in {
        "deliveryStatus": r["delivery_status"],
        "deliveryPayload": _loads(r["delivery_payload_json"]),
        "deliveryUpdatedAt": r["delivery_updated_at"],
    }.items():
        if value is not None:
            doc[key] = value
    return doc


def db_insert_message(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO messages (id, conversation_id, contact_id, phone, phone_number_id, display_phone_number,
           direction, type, text, payload_json, status, source, provider_message_id, provider_response_json,
           error_json, error_text, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            doc["id"], doc.get("conversationId"), doc.get("contactId"), doc.get("phone"), doc.get("phoneNumberId"),
            doc.get("displayPhoneNumber"), doc.get("direction"), doc.get("type"), doc.get("text"),
            _dumps(doc.get("payload")) if doc.get("payload") is not None else None, doc.get("status"),
            doc.get("source"), doc.get("providerMessageId"),
            _dumps(doc.get("providerResponse")) if doc.get("providerResponse") is not None else None,
            _dumps(doc.get("error")) if doc.get("error") is not None else None, doc.get("errorText"),
            doc["createdAt"],
        ),
    )


def db_get_messages_for_contact(conn: sqlite3.Connection, contact_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE contact_id = ? ORDER BY created_at ASC, rowid ASC", (contact_id,)
    ).fetchall()
    docs = [_message_row_to_dict(r) for r in rows]
    return docs[-limit:] if limit else docs


def db_list_inbound_messages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM messages WHERE direction = 'in'").fetchall()
    return [_message_row_to_dict(r) for r in rows]


def db_get_messages_for_conversation(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC", (conversation_id,)
    ).fetchall()
    return [_message_row_to_dict(r) for r in rows]


def db_get_latest_message_per_conversation(conn: sqlite3.Connection, conversation_ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.* FROM messages m
           INNER JOIN (SELECT conversation_id, MAX(rowid) AS max_rowid FROM messages GROUP BY conversation_id) latest
           ON m.conversation_id = latest.conversation_id AND m.rowid = latest.max_rowid"""
    ).fetchall()
    return {r["conversation_id"]: _message_row_to_dict(r) for r in rows}


def db_update_message_delivery(
    conn: sqlite3.Connection, provider_message_id: str, status_name: str, payload: Any, created_at: str,
    readable_error_fn=None,
) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT id FROM messages WHERE provider_message_id = ?", (provider_message_id,)).fetchone()
    if not row:
        return None
    set_clauses = ["delivery_status = ?", "delivery_payload_json = ?", "delivery_updated_at = ?"]
    params: list[Any] = [status_name, _dumps(payload), created_at]
    if status_name == "failed":
        error_value = (payload or {}).get("errors")
        set_clauses.append("status = 'failed'")
        set_clauses.append("error_json = ?")
        params.append(_dumps(error_value))
        set_clauses.append("error_text = ?")
        params.append(readable_error_fn(error_value) if readable_error_fn else None)
    params.append(row["id"])
    conn.execute(f"UPDATE messages SET {', '.join(set_clauses)} WHERE id = ?", params)
    return db_get_message(conn, row["id"])


def db_get_message(conn: sqlite3.Connection, message_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _message_row_to_dict(row) if row else None


def db_set_message_error_text(conn: sqlite3.Connection, message_id: str, error_text: str) -> None:
    conn.execute("UPDATE messages SET error_text = ? WHERE id = ?", (error_text, message_id))


# ---------------------------------------------------------------------------
# Webhook events
# ---------------------------------------------------------------------------

def db_insert_webhook_event(conn: sqlite3.Connection, event_id: str, payload: dict[str, Any], created_at: str) -> None:
    conn.execute("INSERT INTO webhook_events (id, payload_json, created_at) VALUES (?,?,?)", (event_id, _dumps(payload), created_at))


def db_recent_webhook_events(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM webhook_events ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "payload": _loads(r["payload_json"], {}), "createdAt": r["created_at"]} for r in rows]


def db_count_webhook_events(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def db_insert_media(conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO media (id, filename, content_type, size, path, url, created_at) VALUES (?,?,?,?,?,?,?)",
        (doc["id"], doc.get("filename"), doc.get("contentType"), doc.get("size"), doc.get("path"), doc.get("url"), doc["createdAt"]),
    )


def db_get_media(conn: sqlite3.Connection, media_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "filename": row["filename"], "contentType": row["content_type"], "size": row["size"],
            "path": row["path"], "url": row["url"], "createdAt": row["created_at"]}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def db_get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
    return {r["key"]: _loads(r["value_json"], {}) for r in rows}


def db_get_settings_key(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    return _loads(row["value_json"], {}) if row else {}


def db_set_settings_key(conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, _dumps(value)),
    )


# ---------------------------------------------------------------------------
# Campaigns / campaign results
# ---------------------------------------------------------------------------

_CAMPAIGN_COLUMNS = (
    "id, name, template_name, language, phone_number_id, response_flow_id, scheduled_at, target_count, sent, "
    "failed, delivered, read, button_clicks, last_error_json, last_error_text, config_json, status, created_at, "
    "started_at, last_progress_at, finished_at, failed_at, canceled_at, last_resume_at, last_retry_failed_at, "
    "last_status_at, last_click_at, updated_at"
)


def _campaign_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    doc = {
        "id": r["id"], "name": r["name"], "templateName": r["template_name"], "language": r["language"],
        "phoneNumberId": r["phone_number_id"], "responseFlowId": r["response_flow_id"],
        "scheduledAt": r["scheduled_at"], "targetCount": r["target_count"], "sent": r["sent"],
        "failed": r["failed"], "delivered": r["delivered"], "read": r["read"], "buttonClicks": r["button_clicks"],
        "lastError": _loads(r["last_error_json"]), "lastErrorText": r["last_error_text"],
        "config": _loads(r["config_json"], {}), "status": r["status"], "createdAt": r["created_at"],
    }
    optional_fields = {
        "startedAt": "started_at", "lastProgressAt": "last_progress_at", "finishedAt": "finished_at",
        "failedAt": "failed_at", "canceledAt": "canceled_at", "lastResumeAt": "last_resume_at",
        "lastRetryFailedAt": "last_retry_failed_at", "lastStatusAt": "last_status_at",
        "lastClickAt": "last_click_at", "updatedAt": "updated_at",
    }
    for key, col in optional_fields.items():
        value = r[col]
        if value is not None:
            doc[key] = value
    return doc


def _campaign_result_row_to_dict(r: sqlite3.Row, provider_ids: list[str]) -> dict[str, Any]:
    return {
        "contactId": r["contact_id"], "name": r["name"], "phone": r["phone"], "status": r["status"],
        "messageIds": _loads(r["message_ids_json"], []), "providerMessageIds": provider_ids,
        "sentAt": r["sent_at"], "deliveredAt": r["delivered_at"], "readAt": r["read_at"],
        "clickedAt": r["clicked_at"], "buttonText": r["button_text"], "error": _loads(r["error_json"]),
        "errorText": r["error_text"], "diagnostic": _loads(r["diagnostic_json"]), "createdAt": r["created_at"],
    }


def _results_for_campaigns(conn: sqlite3.Connection, campaign_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Batch-fetches campaign_results (and their provider ids) for one or
    more campaigns in exactly 2 queries total, regardless of how many
    result rows exist -- avoids the N+1 query pattern (one extra query per
    result row) that made /api/campaigns slow under load."""
    if not campaign_ids:
        return {}
    placeholders = ",".join("?" for _ in campaign_ids)
    result_rows = conn.execute(
        f"SELECT * FROM campaign_results WHERE campaign_id IN ({placeholders}) ORDER BY campaign_id, id",
        campaign_ids,
    ).fetchall()
    result_ids = [r["id"] for r in result_rows]
    provider_ids_by_result: dict[int, list[str]] = {rid: [] for rid in result_ids}
    if result_ids:
        p_placeholders = ",".join("?" for _ in result_ids)
        for pr in conn.execute(
            f"SELECT campaign_result_id, provider_message_id FROM campaign_result_provider_ids "
            f"WHERE campaign_result_id IN ({p_placeholders})",
            result_ids,
        ).fetchall():
            provider_ids_by_result[pr["campaign_result_id"]].append(pr["provider_message_id"])

    by_campaign: dict[str, list[dict[str, Any]]] = {cid: [] for cid in campaign_ids}
    for r in result_rows:
        by_campaign[r["campaign_id"]].append(_campaign_result_row_to_dict(r, provider_ids_by_result[r["id"]]))
    return by_campaign


def db_get_campaign(conn: sqlite3.Connection, campaign_id: str, include_results: bool = True) -> Optional[dict[str, Any]]:
    row = conn.execute(f"SELECT {_CAMPAIGN_COLUMNS} FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not row:
        return None
    doc = _campaign_row_to_dict(row)
    if include_results:
        doc["results"] = _results_for_campaigns(conn, [campaign_id]).get(campaign_id, [])
    return doc


def db_list_campaigns(conn: sqlite3.Connection, phone_number_id: Optional[str] = None, include_results: bool = True) -> list[dict[str, Any]]:
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute(f"SELECT {_CAMPAIGN_COLUMNS} FROM campaigns ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_CAMPAIGN_COLUMNS} FROM campaigns WHERE phone_number_id = ? ORDER BY created_at DESC",
            (scope,),
        ).fetchall()
    docs = [_campaign_row_to_dict(r) for r in rows]
    if include_results:
        results_by_campaign = _results_for_campaigns(conn, [d["id"] for d in docs])
        for doc in docs:
            doc["results"] = results_by_campaign.get(doc["id"], [])
    return docs


def db_create_campaign(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO campaigns (id, name, template_name, language, phone_number_id, response_flow_id,
           scheduled_at, target_count, sent, failed, delivered, read, button_clicks, last_error_json,
           last_error_text, config_json, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            doc["id"], doc.get("name"), doc.get("templateName"), doc.get("language"), doc.get("phoneNumberId"),
            doc.get("responseFlowId"), doc.get("scheduledAt"), doc.get("targetCount", 0), doc.get("sent", 0),
            doc.get("failed", 0), doc.get("delivered", 0), doc.get("read", 0), doc.get("buttonClicks", 0),
            _dumps(doc.get("lastError")) if doc.get("lastError") is not None else None, doc.get("lastErrorText"),
            _dumps(doc.get("config") or {}), doc.get("status", "draft"), doc["createdAt"],
        ),
    )
    return db_get_campaign(conn, doc["id"])


def db_update_campaign_fields(conn: sqlite3.Connection, campaign_id: str, fields: dict[str, Any]) -> None:
    """fields keys are Python/JSON-style names (e.g. 'status', 'scheduledAt');
    translated to columns here so callers don't need to know the schema."""
    column_map = {
        "name": "name", "scheduledAt": "scheduled_at", "targetCount": "target_count", "status": "status",
        "startedAt": "started_at", "lastProgressAt": "last_progress_at", "finishedAt": "finished_at",
        "failedAt": "failed_at", "canceledAt": "canceled_at", "lastResumeAt": "last_resume_at",
        "lastRetryFailedAt": "last_retry_failed_at", "lastStatusAt": "last_status_at",
        "lastClickAt": "last_click_at", "lastErrorText": "last_error_text", "updatedAt": "updated_at",
        "configJson": "config_json",
    }
    set_clauses = []
    params: list[Any] = []
    for key, value in fields.items():
        if key == "config":
            set_clauses.append("config_json = ?")
            params.append(_dumps(value))
            continue
        if key == "lastError":
            set_clauses.append("last_error_json = ?")
            params.append(_dumps(value) if value is not None else None)
            continue
        col = column_map.get(key)
        if not col:
            raise KeyError(f"Unknown campaign field: {key}")
        set_clauses.append(f"{col} = ?")
        params.append(value)
    if not set_clauses:
        return
    params.append(campaign_id)
    conn.execute(f"UPDATE campaigns SET {', '.join(set_clauses)} WHERE id = ?", params)


def db_contacts_for_campaign(
    conn: sqlite3.Connection, phone_number_id: Optional[str], list_ids: list[str], tag_ids: list[str], exclusion_list_ids: list[str]
) -> list[dict[str, Any]]:
    """Mirrors contacts_for_campaign(): scoped by phoneNumberId, must match at
    least one of list_ids (if any given) AND at least one of tag_ids (if any
    given), and must NOT be in any of exclusion_list_ids."""
    scope = (phone_number_id or "").strip()
    if not scope:
        rows = conn.execute("SELECT * FROM contacts").fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT c.* FROM contacts c
               LEFT JOIN contact_phone_numbers cpn ON cpn.contact_id = c.id
               WHERE c.last_phone_number_id = ? OR cpn.phone_number_id = ?""",
            (scope, scope),
        ).fetchall()
    contacts = [_contact_row_to_dict(conn, row) for row in rows]
    excluded = set(exclusion_list_ids or [])
    selected = []
    for contact in contacts:
        contact_lists = set(contact.get("lists") or [])
        contact_tags = set(contact.get("tags") or [])
        in_list = not list_ids or bool(contact_lists & set(list_ids))
        in_tag = not tag_ids or bool(contact_tags & set(tag_ids))
        is_excluded = bool(contact_lists & excluded)
        if in_list and in_tag and not is_excluded:
            selected.append(contact)
    return selected


def db_campaign_processed_keys(conn: sqlite3.Connection, campaign_id: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    rows = conn.execute("SELECT contact_id, phone FROM campaign_results WHERE campaign_id = ?", (campaign_id,)).fetchall()
    for row in rows:
        if row["contact_id"]:
            keys.add(("id", str(row["contact_id"])))
        if row["phone"]:
            keys.add(("phone", str(row["phone"])))
    return keys


def db_insert_campaign_result(conn: sqlite3.Connection, campaign_id: str, row: dict[str, Any]) -> int:
    cur = conn.execute(
        """INSERT INTO campaign_results (campaign_id, contact_id, name, phone, status, message_ids_json,
           sent_at, delivered_at, read_at, clicked_at, button_text, error_json, error_text, diagnostic_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            campaign_id, row.get("contactId"), row.get("name"), row.get("phone"), row.get("status"),
            _dumps(row.get("messageIds") or []), row.get("sentAt"), row.get("deliveredAt"), row.get("readAt"),
            row.get("clickedAt"), row.get("buttonText"),
            _dumps(row.get("error")) if row.get("error") is not None else None, row.get("errorText"),
            _dumps(row.get("diagnostic")) if row.get("diagnostic") is not None else None, row.get("createdAt"),
        ),
    )
    result_row_id = cur.lastrowid
    for pmid in dict.fromkeys(row.get("providerMessageIds") or []):
        if pmid:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_result_provider_ids (campaign_result_id, provider_message_id) VALUES (?,?)",
                (result_row_id, pmid),
            )
    return result_row_id


def db_summarize_campaign(conn: sqlite3.Connection, campaign_id: str) -> None:
    """Recomputes campaign aggregate counters from campaign_results via SQL
    aggregation, matching summarize_campaign()'s semantics exactly."""
    agg = conn.execute(
        """SELECT
             SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
             SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered,
             SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) AS read,
             SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS button_clicks
           FROM campaign_results WHERE campaign_id = ?""",
        (campaign_id,),
    ).fetchone()
    last_error_row = conn.execute(
        "SELECT error_json FROM campaign_results WHERE campaign_id = ? AND error_json IS NOT NULL ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    last_error_text_row = conn.execute(
        "SELECT error_text FROM campaign_results WHERE campaign_id = ? AND error_text IS NOT NULL ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    conn.execute(
        """UPDATE campaigns SET sent=?, failed=?, delivered=?, read=?, button_clicks=?, last_error_json=?, last_error_text=?
           WHERE id=?""",
        (
            agg["sent"] or 0, agg["failed"] or 0, agg["delivered"] or 0, agg["read"] or 0, agg["button_clicks"] or 0,
            last_error_row["error_json"] if last_error_row else None,
            last_error_text_row["error_text"] if last_error_text_row else None,
            campaign_id,
        ),
    )


def db_delete_campaign_results_for_contacts(conn: sqlite3.Connection, campaign_id: str, keys: set[tuple[Optional[str], str]]) -> None:
    """Used by retry-failed: removes result rows matching (contactId, normalizedPhone) pairs."""
    rows = conn.execute("SELECT id, contact_id, phone FROM campaign_results WHERE campaign_id = ?", (campaign_id,)).fetchall()
    to_delete = [r["id"] for r in rows if (r["contact_id"], r["phone"] or "") in keys]
    if to_delete:
        conn.executemany("DELETE FROM campaign_results WHERE id = ?", [(i,) for i in to_delete])


def db_update_campaign_delivery_from_status(conn: sqlite3.Connection, status: dict[str, Any], readable_error_fn=None) -> list[str]:
    """Mirrors update_campaign_delivery_from_status(): updates the matching
    message row and every campaign_results row referencing this provider
    message id (via the normalized campaign_result_provider_ids index,
    instead of scanning every campaign's results in Python).
    Returns the list of affected campaign_ids so callers can re-summarize them."""
    provider_id = status.get("providerMessageId")
    status_name = status.get("status")
    if not provider_id or status_name not in {"delivered", "read", "failed", "sent"}:
        return []
    timestamp_col = {"delivered": "delivered_at", "read": "read_at", "sent": "sent_at"}.get(status_name)

    db_update_message_delivery(conn, provider_id, status_name, status.get("payload"), status["createdAt"], readable_error_fn)

    affected_rows = conn.execute(
        """SELECT cr.id, cr.campaign_id, cr.error_json, cr.error_text FROM campaign_results cr
           JOIN campaign_result_provider_ids p ON p.campaign_result_id = cr.id
           WHERE p.provider_message_id = ?""",
        (provider_id,),
    ).fetchall()
    affected_campaign_ids = sorted({r["campaign_id"] for r in affected_rows})
    for r in affected_rows:
        set_clauses = []
        params: list[Any] = []
        if timestamp_col:
            set_clauses.append(f"{timestamp_col} = COALESCE({timestamp_col}, ?)")
            params.append(status["createdAt"])
        if status_name == "failed":
            new_error = (status.get("payload") or {}).get("errors") or _loads(r["error_json"])
            new_error_text = (readable_error_fn(new_error) if readable_error_fn else None) or r["error_text"]
            set_clauses.append("status = 'failed'")
            set_clauses.append("error_json = ?")
            params.append(_dumps(new_error) if new_error is not None else None)
            set_clauses.append("error_text = ?")
            params.append(new_error_text)
        if not set_clauses:
            continue
        params.append(r["id"])
        conn.execute(f"UPDATE campaign_results SET {', '.join(set_clauses)} WHERE id = ?", params)

    for campaign_id in affected_campaign_ids:
        db_summarize_campaign(conn, campaign_id)
        conn.execute("UPDATE campaigns SET last_status_at = ? WHERE id = ?", (status["createdAt"], campaign_id))
    return affected_campaign_ids


def db_update_campaign_click_from_inbound(conn: sqlite3.Connection, inbound: dict[str, Any]) -> Optional[str]:
    button_text = inbound.get("buttonText")
    if not button_text:
        return None
    phone = inbound.get("phone") or ""
    contact_row = conn.execute("SELECT pending_campaign_id FROM contacts WHERE phone = ?", (phone,)).fetchone()
    campaign_id = contact_row["pending_campaign_id"] if contact_row else None
    if not campaign_id:
        return None
    campaign_row = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not campaign_row:
        return None
    result_row = conn.execute(
        "SELECT id FROM campaign_results WHERE campaign_id = ? AND phone = ? ORDER BY id LIMIT 1", (campaign_id, phone)
    ).fetchone()
    if result_row:
        conn.execute(
            "UPDATE campaign_results SET clicked_at = COALESCE(clicked_at, ?), button_text = ? WHERE id = ?",
            (inbound["createdAt"], button_text, result_row["id"]),
        )
    db_summarize_campaign(conn, campaign_id)
    conn.execute("UPDATE campaigns SET last_click_at = ? WHERE id = ?", (inbound["createdAt"], campaign_id))
    return campaign_id
