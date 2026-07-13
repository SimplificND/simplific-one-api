import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
import csv
import io

sys.path.insert(0, str(Path(__file__).parent))
import db as db_module  # noqa: E402


APP_NAME = os.getenv("APP_NAME", "Simplific ONE API")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "change-me")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "/app/storage"))
DB_PATH = STORAGE_DIR / "one-api.db"
LEGACY_JSON_PATH = STORAGE_DIR / "one-api.json"

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _auto_migrate_legacy_json_if_needed() -> None:
    """Safety net for the JSON -> SQLite cutover: if this is the first boot
    against a fresh DB_PATH but a legacy one-api.json still exists (e.g. an
    operator deployed this version without running the migration script
    first), migrate it automatically instead of silently starting empty.
    A no-op on every subsequent boot once one-api.db exists."""
    if DB_PATH.exists():
        return
    if not LEGACY_JSON_PATH.exists():
        return
    migration_dir = Path(__file__).parent / "migration"
    sys.path.insert(0, str(migration_dir))
    from migrate_json_to_sqlite import migrate  # noqa: PLC0415

    print(f"one-api.db not found but {LEGACY_JSON_PATH} exists -- auto-migrating on startup")
    counts = migrate(LEGACY_JSON_PATH, DB_PATH)
    print(f"Auto-migration complete: {counts}")


_auto_migrate_legacy_json_if_needed()
database = db_module.Database(DB_PATH)
STORE_MUTATION_LOCK = asyncio.Lock()


async def db_run(fn: Callable, *args, **kwargs):
    """Runs `fn(conn, *args, **kwargs)` inside a single SQLite transaction on
    a worker thread (matching PR#1's asyncio.to_thread pattern for blocking
    I/O), committing on success and rolling back on any exception."""
    def _run():
        conn = database._connect()
        try:
            result = fn(conn, *args, **kwargs)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return await asyncio.to_thread(_run)


@app.on_event("startup")
async def start_scheduled_workers() -> None:
    # Every db_run() call offloads its SQLite work to asyncio's default
    # executor via asyncio.to_thread. That executor's default size is
    # min(32, cpu_count + 4) -- as few as ~12 threads on a modest box. Under
    # concurrent load (a bulk campaign send + a burst of webhook deliveries,
    # the exact incident this migration is meant to fix) that's nowhere near
    # enough concurrency and becomes its own bottleneck, independent of how
    # fast any individual SQL query is. Widen it explicitly; SQLite access
    # here is brief per call, so a larger pool is safe.
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_event_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))

    await backfill_blacklist_from_button_clicks()
    await resume_running_campaigns_on_startup()
    asyncio.create_task(scheduled_campaign_loop())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def public_base_url() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


class LeadIn(BaseModel):
    name: Optional[str] = None
    phone: str
    tags: list[str] = []
    lists: list[str] = []
    customFields: dict[str, Any] = {}
    phoneNumberId: Optional[str] = None


class ContactUpdateIn(BaseModel):
    name: Optional[str] = None
    tags: list[str] = []
    lists: list[str] = []
    customFields: dict[str, Any] = {}
    phoneNumberId: Optional[str] = None


class NamedIn(BaseModel):
    name: str
    color: Optional[str] = None
    phoneNumberId: Optional[str] = None


class CustomFieldIn(BaseModel):
    key: str
    label: Optional[str] = None
    type: str = "text"
    phoneNumberId: Optional[str] = None


class PhoneNumberManualIn(BaseModel):
    phoneNumberId: str
    displayPhoneNumber: Optional[str] = None
    verifiedName: Optional[str] = None
    qualityRating: Optional[str] = None
    messagingLimitTier: Optional[str] = None


class PhoneNumberRegisterIn(BaseModel):
    pin: str = Field(min_length=4, max_length=64)


class TemplateIn(BaseModel):
    name: str
    language: str = "pt_BR"
    category: str = "UTILITY"
    bodyPreview: Optional[str] = None
    phoneNumberId: Optional[str] = None


class MessageItem(BaseModel):
    type: str = "text"  # text | image | video | audio | document | template
    text: Optional[str] = None
    templateName: Optional[str] = None
    language: str = "pt_BR"
    mediaUrl: Optional[str] = None
    caption: Optional[str] = None
    templateParams: dict[str, Any] = {}
    phoneNumberId: Optional[str] = None
    delaySeconds: int = Field(default=0, ge=0, le=86400)


class SendMessageIn(BaseModel):
    phone: str
    items: list[MessageItem]


class MetaSettingsIn(BaseModel):
    appId: Optional[str] = None
    appSecret: Optional[str] = None
    wabaId: Optional[str] = None
    phoneNumberId: Optional[str] = None
    accessToken: Optional[str] = None
    businessName: Optional[str] = None


class FlowAction(BaseModel):
    type: str = "send_message"  # send_message | image | video | audio | document | add_tags | add_lists | delay
    text: Optional[str] = None
    mediaUrl: Optional[str] = None
    caption: Optional[str] = None
    tags: list[str] = []
    lists: list[str] = []
    delaySeconds: int = Field(default=0, ge=0, le=86400)


class FlowIn(BaseModel):
    name: str
    triggerValue: Optional[str] = None
    enabled: bool = True
    actions: list[FlowAction] = []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    phoneNumberId: Optional[str] = None


class TemplateCampaignIn(BaseModel):
    name: str
    templateName: str
    language: str = "pt_BR"
    listIds: list[str] = []
    tagIds: list[str] = []
    exclusionListIds: list[str] = []
    responseFlowId: Optional[str] = None
    buttonFlowMap: dict[str, str] = {}
    parameterMap: dict[str, str] = {}
    phoneNumberId: Optional[str] = None
    batchSize: int = Field(default=50, ge=1, le=200)
    batchPauseSeconds: int = Field(default=1, ge=0, le=300)
    sendNow: bool = True
    scheduledAt: Optional[str] = None


class CampaignUpdateIn(BaseModel):
    name: Optional[str] = None
    scheduledAt: Optional[str] = None
    batchSize: Optional[int] = Field(default=None, ge=1, le=200)
    batchPauseSeconds: Optional[int] = Field(default=None, ge=0, le=300)
    sendNow: Optional[bool] = None


class AutomationIn(BaseModel):
    name: str
    enabled: bool = True
    triggerType: str = "contains"  # contains | exact | button | any
    triggerValue: str = ""
    addTags: list[str] = []
    addLists: list[str] = []
    items: list[MessageItem] = []
    phoneNumberId: Optional[str] = None


def _configured_meta_tx(conn) -> bool:
    settings = db_module.db_get_settings_key(conn, "meta")
    has_token = settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN")
    phone_numbers = db_module.db_list_phone_numbers(conn)
    has_phone = (
        settings.get("phoneNumberId")
        or os.getenv("META_PHONE_NUMBER_ID")
        or next((p.get("phoneNumberId") or p.get("id") for p in phone_numbers if p.get("active")), "")
        or next(((p.get("phoneNumberId") or p.get("id")) for p in phone_numbers), "")
    )
    return bool(has_token and has_phone)


async def configured_meta() -> bool:
    return await db_run(_configured_meta_tx)


def _meta_config_tx(conn) -> dict[str, str]:
    settings = db_module.db_get_settings_key(conn, "meta")
    return {
        "accessToken": settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN") or "",
        "phoneNumberId": settings.get("phoneNumberId") or os.getenv("META_PHONE_NUMBER_ID") or "",
        "wabaId": settings.get("wabaId") or os.getenv("META_WABA_ID") or "",
        "appId": settings.get("appId") or os.getenv("META_APP_ID") or "",
        "appSecret": settings.get("appSecret") or os.getenv("META_APP_SECRET") or "",
        "businessName": settings.get("businessName") or "",
    }


async def meta_config() -> dict[str, str]:
    return await db_run(_meta_config_tx)


def _active_phone_number_id_tx(conn, phone_numbers: Optional[list[dict[str, Any]]] = None) -> str:
    if phone_numbers is None:
        phone_numbers = db_module.db_list_phone_numbers(conn)
    active = next((p for p in phone_numbers if p.get("active")), None)
    if active:
        return active.get("phoneNumberId") or active.get("id") or ""
    first = next(iter(phone_numbers), None)
    return (first or {}).get("phoneNumberId") or (first or {}).get("id") or ""


async def active_phone_number_id(data: Optional[dict[str, Any]] = None, override: Optional[str] = None) -> str:
    """`data`, if provided, is the legacy full-dict shape some older call
    sites still build up locally (e.g. containing a "phoneNumbers" list) --
    kept for signature compatibility so those call sites don't all need to
    change at once; new call sites should just omit it."""
    if override:
        return override
    cfg = await meta_config()
    if cfg["phoneNumberId"]:
        return cfg["phoneNumberId"]
    if data is not None and "phoneNumbers" in data:
        return await db_run(_active_phone_number_id_tx, data.get("phoneNumbers"))
    return await db_run(_active_phone_number_id_tx, None)


def normalize_scope(phone_number_id: Optional[str]) -> str:
    return (phone_number_id or "").strip()


def scoped(row: dict[str, Any], phone_number_id: Optional[str]) -> bool:
    scope = normalize_scope(phone_number_id)
    if not scope:
        return True
    row_scope = row.get("phoneNumberId")
    if row_scope:
        return row_scope == scope
    row_scopes = row.get("phoneNumberIds") or []
    return scope in row_scopes


def mark_contact_scope(conn, contact_id: str, phone_number_id: Optional[str]) -> None:
    db_module.db_mark_contact_scope(conn, contact_id, phone_number_id, now_iso)


def extract_template_buttons(template: dict[str, Any]) -> list[dict[str, Any]]:
    buttons = []
    for component in template.get("components") or []:
        if component.get("type") == "BUTTONS":
            for index, button in enumerate(component.get("buttons") or []):
                buttons.append({
                    "index": index,
                    "type": button.get("type"),
                    "text": button.get("text") or button.get("phone_number") or button.get("url") or f"Botão {index + 1}",
                })
    return buttons


def extract_template_params(template: dict[str, Any]) -> list[str]:
    params = []
    for component in template.get("components") or []:
        text = component.get("text") or ""
        for match in re.findall(r"\{\{\s*(\d+)\s*\}\}", text):
            if match not in params:
                params.append(match)
    return params


def template_by_name(
    templates: list[dict[str, Any]],
    name: str,
    language: str,
    phone_number_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    matches = [
        t
        for t in templates
        if t.get("name") == name and (t.get("language") == language or not language)
    ]
    if phone_number_id:
        scoped_match = next((t for t in matches if scoped(t, phone_number_id)), None)
        if scoped_match:
            return scoped_match
    return next(iter(matches), None)


def template_preview_text(templates: list[dict[str, Any]], item: MessageItem | dict[str, Any]) -> str:
    payload = item.model_dump() if isinstance(item, MessageItem) else item
    name = payload.get("templateName") or payload.get("template", {}).get("name")
    language = payload.get("language") or (payload.get("template", {}).get("language") or {}).get("code") or ""
    phone_number_id = payload.get("phoneNumberId")
    if not name:
        return ""
    template = template_by_name(templates, name, language, phone_number_id) or template_by_name(templates, name, "", phone_number_id)
    text = (template or {}).get("bodyPreview") or name
    params = payload.get("templateParams") or {}
    for key, value in params.items():
        text = re.sub(r"\{\{\s*" + re.escape(str(key)) + r"\s*\}\}", str(value or ""), text)
    return text


def message_display_text(templates: list[dict[str, Any]], message: dict[str, Any]) -> str:
    if message.get("type") == "template":
        return template_preview_text(templates, message.get("payload") or {}) or message.get("text") or message.get("type") or "-"
    return (
        message.get("text")
        or (message.get("payload") or {}).get("caption")
        or (message.get("payload") or {}).get("mediaUrl")
        or message.get("type")
        or "-"
    )


def with_display_text(templates: list[dict[str, Any]], message: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not message:
        return None
    return {**message, "displayText": message_display_text(templates, message)}


def template_body_components(template_params: dict[str, Any]) -> list[dict[str, Any]]:
    if not template_params:
        return []
    ordered = [template_params[k] for k in sorted(template_params, key=lambda x: int(x) if str(x).isdigit() else str(x))]
    return [{
        "type": "body",
        "parameters": [{"type": "text", "text": str(value or "")} for value in ordered],
    }]


def upsert_contact(conn, phone: str, name: Optional[str] = None, phone_number_id: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    if not normalized:
        raise HTTPException(400, "Telefone inválido")
    return db_module.db_upsert_contact(conn, normalized, name, phone_number_id, new_id, now_iso)


def conversation_for(conn, phone: str, name: Optional[str] = None, phone_number_id: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    return db_module.db_conversation_for(conn, normalized, name, phone_number_id, new_id, now_iso)


def attach_labels(conn, contact: dict[str, Any], tags: list[str], lists: list[str], custom_fields: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Returns the updated contact dict (the passed-in `contact` is NOT
    mutated in place -- it's just a detached snapshot from the DB, so
    callers must use the return value, unlike the old JSON-blob version)."""
    return db_module.db_attach_labels(conn, contact["id"], tags, lists, custom_fields, now_iso)


def resolve_tag_ids(conn, values: list[str], phone_number_id: Optional[str] = None) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        if not value:
            continue
        text = str(value).strip()
        row = conn.execute("SELECT id FROM tags WHERE id = ?", (text,)).fetchone()
        ids.append(row["id"] if row else ensure_named_tag(conn, text, phone_number_id))
    return [item for item in ids if item]


def resolve_list_ids(conn, values: list[str], phone_number_id: Optional[str] = None) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        if not value:
            continue
        text = str(value).strip()
        row = conn.execute("SELECT id FROM lists WHERE id = ?", (text,)).fetchone()
        ids.append(row["id"] if row else ensure_named_list(conn, text, phone_number_id))
    return [item for item in ids if item]


def meta_message_id(response: Any) -> Optional[str]:
    if not isinstance(response, dict):
        return None
    messages = response.get("messages") or []
    if messages and isinstance(messages[0], dict):
        return messages[0].get("id")
    return response.get("id")


def readable_error(error: Any) -> Optional[str]:
    if not error:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, list):
        parts = [readable_error(item) for item in error]
        return " | ".join([part for part in parts if part]) or json.dumps(error, ensure_ascii=False)
    if isinstance(error, dict):
        meta_error = error.get("error") if isinstance(error.get("error"), dict) else error
        details = [
            meta_error.get("message"),
            meta_error.get("error_data", {}).get("details") if isinstance(meta_error.get("error_data"), dict) else None,
            meta_error.get("title"),
            meta_error.get("details"),
        ]
        code = meta_error.get("code")
        subcode = meta_error.get("error_subcode") or meta_error.get("subcode")
        suffix = " ".join([f"code:{code}" if code else "", f"subcode:{subcode}" if subcode else ""]).strip()
        text = " · ".join([str(item) for item in details if item])
        if suffix:
            text = f"{text} ({suffix})" if text else suffix
        return text or json.dumps(error, ensure_ascii=False)
    return str(error)


async def meta_send(item: MessageItem, phone: str) -> dict[str, Any]:
    if not await configured_meta():
        return {"mock": True, "reason": "META_ACCESS_TOKEN or META_PHONE_NUMBER_ID not configured"}

    cfg = await meta_config()
    phone_number_id = item.phoneNumberId or await active_phone_number_id()
    if not phone_number_id:
        raise HTTPException(400, "Nenhum Phone Number ID conectado")
    token = cfg["accessToken"]
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/messages"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(phone),
    }
    if item.type == "template":
        template_payload = {"name": item.templateName, "language": {"code": item.language}}
        components = template_body_components(item.templateParams)
        if components:
            template_payload["components"] = components
        payload.update({
            "type": "template",
            "template": template_payload,
        })
    elif item.type == "text":
        payload.update({"type": "text", "text": {"preview_url": True, "body": item.text or ""}})
    elif item.type in {"image", "video", "audio", "document"}:
        media_body: dict[str, Any] = {"link": item.mediaUrl}
        if item.caption and item.type in {"image", "video", "document"}:
            media_body["caption"] = item.caption
        payload.update({"type": item.type, item.type: media_body})
    else:
        raise HTTPException(400, f"Tipo de mensagem inválido: {item.type}")

    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    return res.json()


def _send_sequence_tx(
    conn, phone: str, sequence_phone_number_id: str, sent_items: list[tuple], source: str
) -> list[dict[str, Any]]:
    contact = upsert_contact(conn, phone, phone_number_id=sequence_phone_number_id)
    convo = conversation_for(conn, phone, contact.get("name"), sequence_phone_number_id)
    templates = db_module.db_list_templates(conn)
    results: list[dict[str, Any]] = []
    for item, status, response, error, created_at in sent_items:
        msg = {
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "phoneNumberId": item.phoneNumberId or sequence_phone_number_id,
            "direction": "out",
            "type": item.type,
            "text": template_preview_text(templates, item) if item.type == "template" else (item.text or item.caption or item.mediaUrl),
            "payload": item.model_dump(),
            "status": status,
            "source": source,
            "providerMessageId": meta_message_id(response),
            "providerResponse": response,
            "error": error,
            "errorText": readable_error(error),
            "createdAt": created_at,
        }
        db_module.db_insert_message(conn, msg)
        results.append(msg)
    db_module.db_update_conversation_on_message(conn, convo["id"], now_iso())
    return results


async def send_sequence(phone: str, items: list[MessageItem], source: str = "manual") -> list[dict[str, Any]]:
    sequence_phone_number_id = next((item.phoneNumberId for item in items if item.phoneNumberId), None) or await active_phone_number_id()
    sent_items: list[tuple[MessageItem, str, Any, Any, str]] = []
    for index, item in enumerate(items):
        if not item.phoneNumberId and sequence_phone_number_id:
            item.phoneNumberId = sequence_phone_number_id
        if item.delaySeconds:
            await asyncio.sleep(item.delaySeconds)
        status = "sent"
        response: Any = None
        error: Any = None
        try:
            response = await meta_send(item, phone)
        except HTTPException as e:
            status = "failed"
            error = e.detail
        sent_items.append((item, status, response, error, now_iso()))

    async with STORE_MUTATION_LOCK:
        return await db_run(_send_sequence_tx, phone, sequence_phone_number_id, sent_items, source)


def _set_pending_response_flow_tx(
    conn, phone: str, response_flow_id: Optional[str], button_flow_map: Optional[dict[str, str]],
    campaign_id: Optional[str], phone_number_id: Optional[str],
) -> None:
    contact = upsert_contact(conn, phone, phone_number_id=phone_number_id)
    db_module.db_set_contact_pending(conn, contact["id"], response_flow_id, button_flow_map, campaign_id)


async def set_pending_response_flow(phone: str, response_flow_id: Optional[str] = None, button_flow_map: Optional[dict[str, str]] = None, campaign_id: Optional[str] = None, phone_number_id: Optional[str] = None) -> None:
    async with STORE_MUTATION_LOCK:
        await db_run(_set_pending_response_flow_tx, phone, response_flow_id, button_flow_map, campaign_id, phone_number_id)


def ensure_named_list(conn, name: str, phone_number_id: Optional[str] = None) -> str:
    return db_module.db_ensure_named_list(conn, name, phone_number_id, new_id, now_iso)


def ensure_named_tag(conn, name: str, phone_number_id: Optional[str] = None) -> str:
    return db_module.db_ensure_named_tag(conn, name, phone_number_id, new_id, now_iso)


def inbound_button_text(message: dict[str, Any]) -> str:
    payload = message.get("payload") or {}
    interactive = payload.get("interactive") or {}
    return str(
        message.get("text")
        or (payload.get("button") or {}).get("text")
        or (interactive.get("button_reply") or {}).get("title")
        or (interactive.get("list_reply") or {}).get("title")
        or ""
    ).strip()


def is_blacklist_button_text(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized in {"bloquear contato", "blacklist"} or "bloquear contato" in normalized


def _backfill_blacklist_tx(conn) -> int:
    changed = 0
    for message in db_module.db_list_inbound_messages(conn):
        if not is_blacklist_button_text(inbound_button_text(message)):
            continue
        contact = None
        if message.get("contactId"):
            contact = db_module.db_get_contact_by_id(conn, message["contactId"])
        if not contact and message.get("phone"):
            contact = db_module.db_get_contact_by_phone(conn, normalize_phone(str(message.get("phone") or "")))
        if not contact:
            continue
        channel_id = message.get("phoneNumberId") or contact.get("lastPhoneNumberId")
        blacklist_id = ensure_named_list(conn, "Blacklist", channel_id)
        before = set(contact.get("lists") or [])
        updated = attach_labels(conn, contact, [], [blacklist_id])
        if set(updated.get("lists") or []) != before:
            changed += 1
    return changed


async def backfill_blacklist_from_button_clicks() -> None:
    async with STORE_MUTATION_LOCK:
        changed = await db_run(_backfill_blacklist_tx)
    if changed:
        print(f"Applied Blacklist backfill to {changed} contacts")


def _run_flow_plan_tx(conn, phone: str, flow_id: str):
    flow = db_module.db_get_flow(conn, flow_id)
    if not flow or not flow.get("enabled", True):
        return None, None
    flow_phone_number_id = flow.get("phoneNumberId")
    contact = upsert_contact(conn, phone, phone_number_id=flow_phone_number_id)
    return flow, contact


def _run_flow_apply_labels_tx(conn, phone: str, flow_phone_number_id: Optional[str], tag_names: list[str], list_names: list[str]) -> None:
    contact = upsert_contact(conn, phone, phone_number_id=flow_phone_number_id)
    tag_ids = resolve_tag_ids(conn, tag_names, flow_phone_number_id) if tag_names else []
    list_ids = resolve_list_ids(conn, list_names, flow_phone_number_id) if list_names else []
    attach_labels(conn, contact, tag_ids, list_ids)


async def run_flow_for_contact(phone: str, flow_id: str, source: str = "flow") -> dict[str, Any]:
    flow, contact = await db_run(_run_flow_plan_tx, phone, flow_id)
    if not flow:
        return {"skipped": True, "reason": "flow_not_found_or_disabled"}
    flow_phone_number_id = flow.get("phoneNumberId") or contact.get("lastPhoneNumberId") or await active_phone_number_id()
    sent_items: list[MessageItem] = []
    tag_names: list[str] = []
    list_names: list[str] = []
    # This pass only builds the plan (which tags/lists to add, which messages
    # to queue) and executes any configured delays between steps. It never
    # held the store lock even before this fix (delaySeconds can be up to
    # 24h per FlowAction) -- the actual write always happened once, at the
    # very end, after all delays. We keep that property, but re-read fresh
    # state right before the write (below) so a long delay here can't clobber
    # a concurrent update made to this contact in the meantime.
    for action in flow.get("actions") or []:
        action_type = action.get("type")
        if action_type == "delay":
            await asyncio.sleep(int(action.get("delaySeconds") or 0))
        elif action_type == "add_tags":
            tag_names.extend(action.get("tags") or [])
        elif action_type == "add_lists":
            list_names.extend(action.get("lists") or [])
        elif action_type == "send_message":
            sent_items.append(MessageItem(type="text", text=action.get("text"), phoneNumberId=flow_phone_number_id, delaySeconds=int(action.get("delaySeconds") or 0)))
        elif action_type in {"image", "video", "audio", "document"}:
            sent_items.append(MessageItem(type=action_type, mediaUrl=action.get("mediaUrl"), caption=action.get("caption"), phoneNumberId=flow_phone_number_id, delaySeconds=int(action.get("delaySeconds") or 0)))

    if tag_names or list_names:
        async with STORE_MUTATION_LOCK:
            await db_run(_run_flow_apply_labels_tx, phone, flow_phone_number_id, tag_names, list_names)

    results = await send_sequence(phone, sent_items, source=source) if sent_items else []
    return {"flowId": flow_id, "messages": len(results)}


def contacts_for_campaign(conn, body: TemplateCampaignIn) -> list[dict[str, Any]]:
    return db_module.db_contacts_for_campaign(conn, body.phoneNumberId, body.listIds, body.tagIds, body.exclusionListIds)


def campaign_processed_keys(conn, campaign_id: str) -> set[tuple[str, str]]:
    return db_module.db_campaign_processed_keys(conn, campaign_id)


def summarize_campaign(conn, campaign_id: str) -> None:
    db_module.db_summarize_campaign(conn, campaign_id)


async def execute_campaign_contact(campaign_id: str, body: TemplateCampaignIn, contact: dict[str, Any]) -> dict[str, Any]:
    custom_fields = contact.get("customFields") or {}
    template_params = {
        key: custom_fields.get(field_key, contact.get(field_key, ""))
        for key, field_key in (body.parameterMap or {}).items()
    }
    item = MessageItem(
        type="template",
        templateName=body.templateName,
        language=body.language,
        templateParams=template_params,
        phoneNumberId=body.phoneNumberId,
    )
    status = "sent"
    response: Any = None
    row_error: Any = None
    try:
        response = await meta_send(item, contact["phone"])
    except HTTPException as exc:
        status = "failed"
        row_error = exc.detail
    row_error_text = readable_error(row_error)
    if status != "sent" and not row_error_text:
        row_error_text = (
            "Envio nao foi aceito, mas a Meta nao retornou motivo. "
            "Verifique se o template esta aprovado no idioma escolhido, se todos os parametros "
            "obrigatorios foram preenchidos, se o telefone tem DDI e se o Phone Number ID esta correto."
        )
    return {
        "item": item,
        "contact": contact,
        "status": status,
        "response": response,
        "error": row_error,
        "errorText": row_error_text,
        "templateParams": template_params,
        "createdAt": now_iso(),
    }


def _persist_campaign_batch_tx(conn, campaign_id: str, body: TemplateCampaignIn, results: list[dict[str, Any]], target_count: int) -> None:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if not campaign:
        return
    for result in results:
        contact = result["contact"]
        item = result["item"]
        phone_number_id = item.phoneNumberId or body.phoneNumberId or _active_phone_number_id_tx(conn)
        stored_contact = upsert_contact(conn, contact["phone"], contact.get("name"), phone_number_id)
        convo = conversation_for(conn, contact["phone"], stored_contact.get("name"), phone_number_id)
        templates = db_module.db_list_templates(conn)
        msg = {
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": stored_contact["id"],
            "phone": stored_contact["phone"],
            "phoneNumberId": phone_number_id,
            "direction": "out",
            "type": item.type,
            "text": template_preview_text(templates, item),
            "payload": item.model_dump(),
            "status": result["status"],
            "source": f"campaign:{campaign_id}",
            "providerMessageId": meta_message_id(result["response"]),
            "providerResponse": result["response"],
            "error": result["error"],
            "errorText": result["errorText"],
            "createdAt": result["createdAt"],
        }
        db_module.db_insert_message(conn, msg)
        db_module.db_update_conversation_on_message(conn, convo["id"], now_iso())
        row = {
            "contactId": contact.get("id"),
            "name": contact.get("name"),
            "phone": contact.get("phone"),
            "status": result["status"],
            "messageIds": [msg["id"]],
            "providerMessageIds": [msg["providerMessageId"]] if msg.get("providerMessageId") else [],
            "sentAt": result["createdAt"] if result["status"] == "sent" else None,
            "deliveredAt": None,
            "readAt": None,
            "clickedAt": None,
            "buttonText": None,
            "error": result["error"],
            "errorText": result["errorText"],
            "diagnostic": {
                "campaignId": campaign_id,
                "templateName": body.templateName,
                "language": body.language,
                "phoneNumberId": phone_number_id,
                "phone": contact.get("phone"),
                "templateParams": result["templateParams"],
            },
            "createdAt": now_iso(),
        }
        db_module.db_insert_campaign_result(conn, campaign_id, row)
        db_module.db_set_contact_pending(
            conn, stored_contact["id"],
            response_flow_id=body.responseFlowId if not body.buttonFlowMap else None,
            button_flow_map=body.buttonFlowMap or None,
            campaign_id=campaign_id,
        )
    db_module.db_update_campaign_fields(conn, campaign_id, {"targetCount": target_count, "status": "running", "lastProgressAt": now_iso()})
    db_module.db_summarize_campaign(conn, campaign_id)


async def persist_campaign_batch(campaign_id: str, body: TemplateCampaignIn, results: list[dict[str, Any]], target_count: int) -> None:
    async with STORE_MUTATION_LOCK:
        await db_run(_persist_campaign_batch_tx, campaign_id, body, results, target_count)


def _execute_campaign_start_tx(conn, campaign_id: str) -> Optional[dict[str, Any]]:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if not campaign:
        return None
    updates = {"status": "running", "lastProgressAt": now_iso()}
    if not campaign.get("startedAt"):
        updates["startedAt"] = now_iso()
    db_module.db_update_campaign_fields(conn, campaign_id, updates)
    db_module.db_summarize_campaign(conn, campaign_id)
    return db_module.db_get_campaign(conn, campaign_id, include_results=True)


def _execute_campaign_contacts_tx(conn, body: TemplateCampaignIn) -> list[dict[str, Any]]:
    return contacts_for_campaign(conn, body)


def _campaign_processed_keys_tx(conn, campaign_id: str) -> set[tuple[str, str]]:
    return campaign_processed_keys(conn, campaign_id)


def _campaign_status_tx(conn, campaign_id: str) -> Optional[str]:
    row = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    return row.get("status") if row else None


def _execute_campaign_fail_tx(conn, campaign_id: str, error_text: str) -> None:
    db_module.db_update_campaign_fields(conn, campaign_id, {"status": "failed", "lastErrorText": error_text, "failedAt": now_iso()})
    db_module.db_summarize_campaign(conn, campaign_id)


def _execute_campaign_done_tx(conn, campaign_id: str, target_count: int) -> None:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if campaign:
        db_module.db_update_campaign_fields(conn, campaign_id, {"status": "done", "targetCount": target_count, "finishedAt": now_iso()})
        db_module.db_summarize_campaign(conn, campaign_id)


async def execute_campaign(campaign_id: str, resume: bool = False) -> None:
    async with STORE_MUTATION_LOCK:
        campaign = await db_run(_execute_campaign_start_tx, campaign_id)
    if not campaign:
        return

    body = TemplateCampaignIn(**campaign["config"])
    contacts = await db_run(_execute_campaign_contacts_tx, body)
    processed = await db_run(_campaign_processed_keys_tx, campaign_id) if resume else set()
    pending_contacts = []
    for contact in contacts:
        contact_keys = {("id", str(contact.get("id"))), ("phone", normalize_phone(str(contact.get("phone") or "")))}
        if processed and contact_keys & processed:
            continue
        pending_contacts.append(contact)
    batch_size = max(1, min(int(body.batchSize or 50), 100))
    batch_pause = max(0, min(int(body.batchPauseSeconds or 0), 300))
    try:
        for start in range(0, len(pending_contacts), batch_size):
            current_status = await db_run(_campaign_status_tx, campaign_id)
            if current_status == "canceled":
                return
            batch = pending_contacts[start:start + batch_size]
            batch_results = await asyncio.gather(*(execute_campaign_contact(campaign_id, body, contact) for contact in batch))
            await persist_campaign_batch(campaign_id, body, batch_results, len(contacts))
            if batch_pause and start + batch_size < len(pending_contacts):
                await asyncio.sleep(batch_pause)
    except Exception as exc:
        async with STORE_MUTATION_LOCK:
            await db_run(_execute_campaign_fail_tx, campaign_id, str(exc))
        return

    async with STORE_MUTATION_LOCK:
        await db_run(_execute_campaign_done_tx, campaign_id, len(contacts))


def _mark_campaign_resuming_tx(conn, campaign_id: str) -> dict[str, Any]:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") == "canceled":
        raise HTTPException(status_code=400, detail="Campanha cancelada")
    db_module.db_update_campaign_fields(conn, campaign_id, {"status": "running", "lastResumeAt": now_iso(), "lastErrorText": None})
    return db_module.db_get_campaign(conn, campaign_id, include_results=True)


async def mark_campaign_resuming(campaign_id: str) -> dict[str, Any]:
    """Shared state transition used by both the manual resume endpoint and the
    startup auto-resume scan, so both paths behave identically."""
    async with STORE_MUTATION_LOCK:
        return await db_run(_mark_campaign_resuming_tx, campaign_id)


def _list_running_campaign_ids_tx(conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM campaigns WHERE status = 'running'").fetchall()]


async def resume_running_campaigns_on_startup() -> None:
    """Scan for campaigns that were left in 'running' status (e.g. the process
    crashed or the container restarted mid-send) and resume them automatically,
    using the exact same code path as POST /api/campaigns/{id}/resume."""
    running_ids = await db_run(_list_running_campaign_ids_tx)
    for campaign_id in running_ids:
        try:
            await mark_campaign_resuming(campaign_id)
        except HTTPException as exc:
            print(f"Skipped auto-resume for campaign {campaign_id}: {exc.detail}")
            continue
        asyncio.create_task(execute_campaign(campaign_id, resume=True))
        print(f"Auto-resumed campaign {campaign_id} on startup")
    if running_ids:
        print(f"Startup auto-resume: found {len(running_ids)} running campaign(s)")


def _list_due_scheduled_campaign_ids_tx(conn) -> list[str]:
    rows = conn.execute("SELECT id, scheduled_at FROM campaigns WHERE status = 'scheduled'").fetchall()
    now = datetime.now(timezone.utc)
    due = []
    for r in rows:
        dt = parse_datetime(r["scheduled_at"])
        if dt and dt <= now:
            due.append(r["id"])
    return due


async def scheduled_campaign_loop() -> None:
    while True:
        try:
            due = await db_run(_list_due_scheduled_campaign_ids_tx)
            for campaign_id in due:
                await execute_campaign(campaign_id)
        except Exception:
            pass
        await asyncio.sleep(30)


def automation_matches(automation: dict[str, Any], inbound: dict[str, Any]) -> bool:
    if not automation.get("enabled", True):
        return False
    trigger_type = automation.get("triggerType") or "contains"
    trigger_value = str(automation.get("triggerValue") or "").strip().lower()
    text = str(inbound.get("text") or inbound.get("buttonText") or "").strip().lower()
    if trigger_type == "any":
        return True
    if not trigger_value:
        return False
    if trigger_type == "exact":
        return text == trigger_value
    if trigger_type == "button":
        return str(inbound.get("buttonText") or "").strip().lower() == trigger_value
    return trigger_value in text


def _run_matching_automations_clear_pending_tx(conn, phone: str, inbound: dict[str, Any]) -> Optional[str]:
    channel_id = inbound.get("phoneNumberId")
    contact = upsert_contact(conn, phone, inbound.get("name"), channel_id)
    button_text = str(inbound.get("buttonText") or inbound.get("text") or "").strip()
    pending_map = contact.get("pendingResponseFlows") or {}
    pending_flow = pending_map.get(button_text) or contact.get("pendingResponseFlowId")
    if pending_flow:
        db_module.db_clear_contact_pending_flow(conn, contact["id"])
    return pending_flow


def _run_matching_automations_apply_tx(conn, phone: str, inbound: dict[str, Any]) -> tuple[str, list[tuple[str, list[dict]]]]:
    channel_id = inbound.get("phoneNumberId")
    contact = upsert_contact(conn, phone, inbound.get("name"), channel_id)
    automations = db_module.db_list_automations(conn, channel_id)
    matches = [a for a in automations if automation_matches(a, inbound)]
    pending_sends: list[tuple[str, list[dict]]] = []
    for automation in matches:
        tag_ids = resolve_tag_ids(conn, automation.get("addTags") or [], channel_id)
        list_ids = resolve_list_ids(conn, automation.get("addLists") or [], channel_id)
        contact = attach_labels(conn, contact, tag_ids, list_ids)
        db_module.db_insert_automation_run(conn, {
            "id": new_id("run"),
            "automationId": automation["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "trigger": inbound,
            "createdAt": now_iso(),
        })
        items = automation.get("items") or []
        if items:
            pending_sends.append((automation["id"], items))
    return contact["phone"], pending_sends


async def run_matching_automations(phone: str, inbound: dict[str, Any]) -> None:
    async with STORE_MUTATION_LOCK:
        pending_flow = await db_run(_run_matching_automations_clear_pending_tx, phone, inbound)

    if pending_flow:
        # run_flow_for_contact acquires STORE_MUTATION_LOCK itself -- must not
        # be called while we're already holding it (asyncio.Lock isn't
        # reentrant, that would deadlock).
        await run_flow_for_contact(phone, pending_flow, source=f"button-flow:{pending_flow}")

    async with STORE_MUTATION_LOCK:
        contact_phone, pending_sends = await db_run(_run_matching_automations_apply_tx, phone, inbound)

    for automation_id, items in pending_sends:
        # send_sequence also acquires STORE_MUTATION_LOCK itself -- called
        # here, after the block above has released it.
        items_models = [MessageItem(**item) for item in items]
        await send_sequence(contact_phone, items_models, source=f"automation:{automation_id}")


def extract_webhook_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")
            display_phone_number = metadata.get("display_phone_number")
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", []) or []}
            for raw in value.get("messages", []) or []:
                phone = raw.get("from")
                msg_type = raw.get("type")
                text = None
                button_text = None
                if msg_type == "text":
                    text = (raw.get("text") or {}).get("body")
                elif msg_type == "button":
                    button_text = (raw.get("button") or {}).get("text")
                    text = button_text
                elif msg_type == "interactive":
                    interactive = raw.get("interactive") or {}
                    button_text = ((interactive.get("button_reply") or {}).get("title") or (interactive.get("list_reply") or {}).get("title"))
                    text = button_text
                messages.append({
                    "providerId": raw.get("id"),
                    "phone": phone,
                    "phoneNumberId": phone_number_id,
                    "displayPhoneNumber": display_phone_number,
                    "name": contacts.get(phone),
                    "type": msg_type,
                    "text": text,
                    "buttonText": button_text,
                    "payload": raw,
                    "createdAt": now_iso(),
                })
    return messages


def extract_webhook_statuses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            for raw in value.get("statuses", []) or []:
                statuses.append({
                    "providerMessageId": raw.get("id"),
                    "status": raw.get("status"),
                    "phone": (raw.get("recipient_id") or ""),
                    "phoneNumberId": metadata.get("phone_number_id"),
                    "displayPhoneNumber": metadata.get("display_phone_number"),
                    "payload": raw,
                    "createdAt": now_iso(),
                })
    return statuses


def summarize_webhook_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    phone_numbers: list[str] = []
    display_numbers: list[str] = []
    fields: list[str] = []
    sample_messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            field = change.get("field")
            if field:
                fields.append(field)
            metadata = value.get("metadata") or {}
            phone_id = metadata.get("phone_number_id")
            display_phone = metadata.get("display_phone_number")
            if phone_id and phone_id not in phone_numbers:
                phone_numbers.append(phone_id)
            if display_phone and display_phone not in display_numbers:
                display_numbers.append(display_phone)
            for raw in value.get("messages", []) or []:
                sample_messages.append({
                    "from": raw.get("from"),
                    "type": raw.get("type"),
                    "text": (raw.get("text") or {}).get("body"),
                    "id": raw.get("id"),
                })
    inbound_messages = extract_webhook_messages(payload)
    delivery_statuses = extract_webhook_statuses(payload)
    return {
        "id": event.get("id"),
        "createdAt": event.get("createdAt"),
        "fields": fields,
        "phoneNumberIds": phone_numbers,
        "displayPhoneNumbers": display_numbers,
        "messages": len(inbound_messages),
        "statuses": len(delivery_statuses),
        "sampleMessages": sample_messages[:3],
    }


def update_campaign_delivery_from_status(conn, status: dict[str, Any]) -> None:
    """Delegates to db_module, which mirrors the original in-place-mutation
    logic but via the indexed campaign_result_provider_ids table instead of
    scanning every campaign's results in Python."""
    db_module.db_update_campaign_delivery_from_status(conn, status, readable_error_fn=readable_error)


def update_campaign_click_from_inbound(conn, inbound: dict[str, Any]) -> None:
    db_module.db_update_campaign_click_from_inbound(conn, inbound)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    cfg = await meta_config()
    return {
        "ok": True,
        "app": APP_NAME,
        "time": now_iso(),
        "metaConfigured": await configured_meta(),
        "phoneNumberId": bool(cfg["phoneNumberId"]),
        "wabaId": bool(cfg["wabaId"]),
        "webhookToken": META_VERIFY_TOKEN != "change-me",
    }


def _dashboard_tx(conn, phone_number_id: Optional[str]) -> dict[str, Any]:
    scope = (phone_number_id or "").strip()
    contacts = db_module.db_list_contacts(conn, phone_number_id)
    lists = db_module.db_list_lists(conn, phone_number_id)
    tags = db_module.db_list_tags(conn, phone_number_id)
    templates = db_module.db_list_templates(conn, phone_number_id)
    campaigns = db_module.db_list_campaigns(conn, phone_number_id, include_results=False)
    conversations = db_module.db_list_conversations(conn, phone_number_id)
    if scope:
        messages_rows = conn.execute("SELECT direction, status FROM messages WHERE phone_number_id = ?", (scope,)).fetchall()
        # automationRuns rows never carry a phoneNumberId/phoneNumberIds field
        # (see AutomationRun's shape), so scoped() -- applied in the old
        # JSON-blob code -- always returned False for them whenever a scope
        # was requested. Preserved here exactly (not "fixed"), since this PR
        # is a storage migration, not a behavior change.
        automation_runs_count = 0
    else:
        messages_rows = conn.execute("SELECT direction, status FROM messages").fetchall()
        automation_runs_count = conn.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0]
    unread = sum(1 for c in conversations if (c.get("unread") or 0) > 0)
    sent = sum(1 for m in messages_rows if m["direction"] == "out" and m["status"] == "sent")
    failed = sum(1 for m in messages_rows if m["direction"] == "out" and m["status"] == "failed")
    return {
        "contacts": len(contacts),
        "lists": len(lists),
        "tags": len(tags),
        "templates": len(templates),
        "campaigns": len(campaigns),
        "inboxUnread": unread,
        "automationRuns": automation_runs_count,
        "messagesSent": sent,
        "messagesFailed": failed,
    }


@app.get("/api/dashboard")
async def dashboard(phoneNumberId: Optional[str] = None) -> dict[str, Any]:
    return await db_run(_dashboard_tx, phoneNumberId)


@app.get("/api/meta/webhooks/recent")
async def recent_webhooks(limit: int = 25) -> dict[str, Any]:
    events = await db_run(lambda conn: db_module.db_recent_webhook_events(conn, max(1, min(limit, 100))))
    return {
        "count": len(events),
        "events": [summarize_webhook_event(event) for event in events],
    }


@app.get("/api/meta/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN and hub_challenge:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Webhook verification failed")


def _receive_webhook_tx(conn, payload: dict[str, Any], inbound_messages: list[dict[str, Any]], delivery_statuses: list[dict[str, Any]]) -> None:
    db_module.db_insert_webhook_event(conn, new_id("evt"), payload, now_iso())
    for status in delivery_statuses:
        update_campaign_delivery_from_status(conn, status)
    for inbound in inbound_messages:
        channel_id = inbound.get("phoneNumberId")
        contact = upsert_contact(conn, inbound["phone"], inbound.get("name"), channel_id)
        convo = conversation_for(conn, contact["phone"], contact.get("name"), channel_id)
        db_module.db_update_conversation_on_message(
            conn, convo["id"], inbound["createdAt"],
            phone_number_id=channel_id, display_phone_number=inbound.get("displayPhoneNumber"),
            inbound=True,
        )
        db_module.db_insert_message(conn, {
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "phoneNumberId": channel_id,
            "displayPhoneNumber": inbound.get("displayPhoneNumber"),
            "direction": "in",
            "type": inbound.get("type"),
            "text": inbound.get("text"),
            "payload": inbound.get("payload"),
            "status": "received",
            "createdAt": inbound["createdAt"],
        })
        update_campaign_click_from_inbound(conn, inbound)


@app.post("/api/meta/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    payload = await request.json()
    inbound_messages = extract_webhook_messages(payload)
    delivery_statuses = extract_webhook_statuses(payload)
    async with STORE_MUTATION_LOCK:
        await db_run(_receive_webhook_tx, payload, inbound_messages, delivery_statuses)
    for inbound in inbound_messages:
        background.add_task(run_matching_automations, inbound["phone"], inbound)
    return {"received": True, "time": now_iso(), "messages": len(inbound_messages), "statuses": len(delivery_statuses)}


@app.get("/api/settings")
async def settings() -> dict[str, Any]:
    all_settings = await db_run(db_module.db_get_settings)
    meta = {**await meta_config()}
    if meta.get("accessToken"):
        meta["accessTokenPreview"] = f"{meta['accessToken'][:8]}...{meta['accessToken'][-4:]}" if len(meta["accessToken"]) > 14 else "***"
        meta["accessToken"] = ""
    return {"metaGraphVersion": META_GRAPH_VERSION, "meta": meta, **all_settings}


def _save_meta_settings_tx(conn, update: dict[str, Any]) -> None:
    current = db_module.db_get_settings_key(conn, "meta")
    current.update({k: v for k, v in update.items() if v not in (None, "")})
    current["updatedAt"] = now_iso()
    db_module.db_set_settings_key(conn, "meta", current)


@app.post("/api/meta/settings")
async def save_meta_settings(body: MetaSettingsIn) -> dict[str, Any]:
    update = body.model_dump()
    if not update.get("accessToken"):
        update.pop("accessToken", None)
    async with STORE_MUTATION_LOCK:
        await db_run(_save_meta_settings_tx, update)
    return {"saved": True, "metaConfigured": await configured_meta()}


def _sync_meta_templates_tx(conn, payload: dict[str, Any], channel_id: Optional[str]) -> list[dict[str, Any]]:
    synced = []
    for tpl in payload.get("data", []) or []:
        body_preview = ""
        for component in tpl.get("components", []) or []:
            if component.get("type") == "BODY":
                body_preview = component.get("text") or ""
        doc = {
            "id": f"meta_tpl_{channel_id or 'global'}_{tpl.get('name')}_{tpl.get('language')}",
            "name": tpl.get("name"),
            "language": tpl.get("language"),
            "status": tpl.get("status"),
            "category": tpl.get("category"),
            "bodyPreview": body_preview,
            "components": tpl.get("components") or [],
            "phoneNumberId": channel_id or None,
            "source": "meta",
            "syncedAt": now_iso(),
        }
        db_module.db_upsert_synced_template(conn, doc)
        synced.append(doc)
    return synced


@app.post("/api/meta/sync-templates")
async def sync_meta_templates() -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para sincronizar modelos.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/message_templates"
    params = {"fields": "name,language,status,category,components"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    payload = res.json()
    channel_id = await active_phone_number_id()
    async with STORE_MUTATION_LOCK:
        synced = await db_run(_sync_meta_templates_tx, payload, channel_id)
    return {"count": len(synced), "templates": synced}


@app.get("/api/meta/subscribed-apps")
async def subscribed_apps() -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para verificar o webhook.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/subscribed_apps"
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    payload = res.json()
    return {"subscribed": bool(payload.get("data")), "apps": payload.get("data") or []}


def _subscribe_webhook_success_tx(conn) -> None:
    meta = db_module.db_get_settings_key(conn, "meta")
    meta["webhookSubscribed"] = True
    meta["webhookSubscribedAt"] = now_iso()
    meta["lastWebhookSubscribeError"] = None
    meta["lastWebhookSubscribeErrorText"] = None
    db_module.db_set_settings_key(conn, "meta", meta)


def _subscribe_webhook_failure_tx(conn, response_payload: Any) -> None:
    meta = db_module.db_get_settings_key(conn, "meta")
    meta["webhookSubscribed"] = False
    meta["lastWebhookSubscribeError"] = response_payload
    meta["lastWebhookSubscribeErrorText"] = readable_error(response_payload) or str(response_payload)
    meta["lastWebhookSubscribeErrorAt"] = now_iso()
    db_module.db_set_settings_key(conn, "meta", meta)


@app.post("/api/meta/subscribe-webhook")
async def subscribe_webhook() -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para ativar o webhook.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/subscribed_apps"
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    response_payload: Any = res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text
    async with STORE_MUTATION_LOCK:
        if res.status_code >= 400:
            await db_run(_subscribe_webhook_failure_tx, response_payload)
            raise HTTPException(res.status_code, response_payload)
        await db_run(_subscribe_webhook_success_tx)
    return {"subscribed": True, "response": response_payload}


@app.get("/api/phone-numbers")
async def list_phone_numbers() -> list[dict[str, Any]]:
    numbers = await db_run(db_module.db_list_phone_numbers)
    cfg = await meta_config()
    if cfg["phoneNumberId"] and not any((p.get("phoneNumberId") or p.get("id")) == cfg["phoneNumberId"] for p in numbers):
        numbers.append({
            "id": cfg["phoneNumberId"],
            "phoneNumberId": cfg["phoneNumberId"],
            "displayPhoneNumber": "",
            "verifiedName": cfg.get("businessName") or "",
            "qualityRating": "UNKNOWN",
            "messagingLimitTier": "UNKNOWN",
            "active": True,
            "source": "settings",
        })
    return numbers


def _add_phone_number_tx(conn, doc: dict[str, Any]) -> dict[str, Any]:
    if not doc.get("active"):
        existing_any = conn.execute("SELECT COUNT(*) FROM phone_numbers").fetchone()[0]
        doc["active"] = existing_any == 0
    return db_module.db_upsert_phone_number(conn, doc)


@app.post("/api/phone-numbers")
async def add_phone_number(body: PhoneNumberManualIn) -> dict[str, Any]:
    doc = {
        "id": body.phoneNumberId,
        "phoneNumberId": body.phoneNumberId,
        "displayPhoneNumber": body.displayPhoneNumber,
        "verifiedName": body.verifiedName,
        "qualityRating": body.qualityRating or "UNKNOWN",
        "messagingLimitTier": body.messagingLimitTier or "UNKNOWN",
        "source": "manual",
        "createdAt": now_iso(),
    }
    async with STORE_MUTATION_LOCK:
        return await db_run(_add_phone_number_tx, doc)


def _sync_phone_numbers_tx(conn, payload: dict[str, Any], active_id: str) -> list[dict[str, Any]]:
    synced = []
    for row in payload.get("data", []) or []:
        doc = {
            "id": row.get("id"),
            "phoneNumberId": row.get("id"),
            "displayPhoneNumber": row.get("display_phone_number"),
            "verifiedName": row.get("verified_name"),
            "qualityRating": row.get("quality_rating") or "UNKNOWN",
            "messagingLimitTier": row.get("messaging_limit_tier") or "UNKNOWN",
            "codeVerificationStatus": row.get("code_verification_status"),
            "nameStatus": row.get("name_status"),
            "active": row.get("id") == active_id,
            "source": "meta",
            "syncedAt": now_iso(),
        }
        existing = db_module.db_get_phone_number(conn, row.get("id"))
        if not existing:
            doc["createdAt"] = now_iso()
        db_module.db_upsert_phone_number(conn, doc)
        synced.append(doc)
    return synced


@app.post("/api/phone-numbers/sync")
async def sync_phone_numbers() -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para sincronizar números.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/phone_numbers"
    params = {"fields": "id,display_phone_number,verified_name,quality_rating,messaging_limit_tier,code_verification_status,name_status"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    payload = res.json()
    active_id = await active_phone_number_id()
    async with STORE_MUTATION_LOCK:
        synced = await db_run(_sync_phone_numbers_tx, payload, active_id)
    return {"count": len(synced), "phoneNumbers": synced}


def _refresh_phone_number_tx(conn, phone_number_id: str, row: dict[str, Any]) -> dict[str, Any]:
    existing = db_module.db_get_phone_number(conn, phone_number_id)
    if not existing:
        meta = db_module.db_get_settings_key(conn, "meta")
        existing = {
            "id": phone_number_id,
            "phoneNumberId": phone_number_id,
            "active": meta.get("phoneNumberId") == phone_number_id,
            "source": "meta",
            "createdAt": now_iso(),
        }
    existing.update({
        "id": row.get("id") or phone_number_id,
        "phoneNumberId": row.get("id") or phone_number_id,
        "displayPhoneNumber": row.get("display_phone_number") or existing.get("displayPhoneNumber"),
        "verifiedName": row.get("verified_name") or existing.get("verifiedName"),
        "qualityRating": row.get("quality_rating") or "UNKNOWN",
        "messagingLimitTier": row.get("messaging_limit_tier") or "UNKNOWN",
        "codeVerificationStatus": row.get("code_verification_status"),
        "nameStatus": row.get("name_status"),
        "refreshedAt": now_iso(),
    })
    return db_module.db_upsert_phone_number(conn, existing)


@app.post("/api/phone-numbers/{phone_number_id}/refresh")
async def refresh_phone_number(phone_number_id: str) -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"]:
        raise HTTPException(400, "Configure Access Token para atualizar o número.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}"
    params = {"fields": "id,display_phone_number,verified_name,quality_rating,messaging_limit_tier,code_verification_status,name_status"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    row = res.json()
    async with STORE_MUTATION_LOCK:
        return await db_run(_refresh_phone_number_tx, phone_number_id, row)


def _register_phone_number_tx(conn, phone_number_id: str, res_status: int, response_payload: Any) -> dict[str, Any]:
    existing = db_module.db_get_phone_number(conn, phone_number_id)
    if not existing:
        meta = db_module.db_get_settings_key(conn, "meta")
        existing = {
            "id": phone_number_id,
            "phoneNumberId": phone_number_id,
            "active": meta.get("phoneNumberId") == phone_number_id,
            "source": "meta",
            "createdAt": now_iso(),
        }

    if res_status >= 400:
        existing.update({
            "registrationStatus": "failed",
            "registered": False,
            "lastRegistrationError": response_payload,
            "lastRegistrationErrorText": readable_error(response_payload) or str(response_payload),
            "lastRegistrationErrorAt": now_iso(),
        })
        db_module.db_upsert_phone_number(conn, existing)
        raise HTTPException(res_status, response_payload)

    existing.update({
        "registrationStatus": "registered",
        "registered": True,
        "registeredAt": now_iso(),
        "lastRegistrationResponse": response_payload,
        "lastRegistrationError": None,
        "lastRegistrationErrorText": None,
    })
    return db_module.db_upsert_phone_number(conn, existing)


@app.post("/api/phone-numbers/{phone_number_id}/register")
async def register_phone_number(phone_number_id: str, body: PhoneNumberRegisterIn) -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"]:
        raise HTTPException(400, "Configure Access Token para registrar o número.")
    pin = (body.pin or "").strip()
    if not pin:
        raise HTTPException(400, "Informe a senha/PIN do número.")

    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/register"
    payload = {"messaging_product": "whatsapp", "pin": pin}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, json=payload, headers={"Authorization": f"Bearer {cfg['accessToken']}"})

    response_payload: Any
    if res.headers.get("content-type", "").startswith("application/json"):
        response_payload = res.json()
    else:
        response_payload = res.text

    async with STORE_MUTATION_LOCK:
        existing = await db_run(_register_phone_number_tx, phone_number_id, res.status_code, response_payload)
    return {"registered": True, "phoneNumber": existing, "response": response_payload}


def _activate_phone_number_tx(conn, phone_number_id: str) -> bool:
    found = db_module.db_activate_phone_number(conn, phone_number_id)
    if found:
        meta = db_module.db_get_settings_key(conn, "meta")
        meta["phoneNumberId"] = phone_number_id
        db_module.db_set_settings_key(conn, "meta", meta)
    return found


@app.post("/api/phone-numbers/{phone_number_id}/activate")
async def activate_phone_number(phone_number_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        found = await db_run(_activate_phone_number_tx, phone_number_id)
    if not found:
        raise HTTPException(404, "Número não encontrado")
    return {"active": phone_number_id}


def _delete_phone_number_tx(conn, phone_number_id: str) -> int:
    deleted = db_module.db_delete_phone_number(conn, phone_number_id)
    meta = db_module.db_get_settings_key(conn, "meta")
    if meta.get("phoneNumberId") == phone_number_id:
        meta.pop("phoneNumberId", None)
        db_module.db_set_settings_key(conn, "meta", meta)
    return deleted


@app.delete("/api/phone-numbers/{phone_number_id}")
async def delete_phone_number(phone_number_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        deleted = await db_run(_delete_phone_number_tx, phone_number_id)
    return {"deleted": deleted}


@app.get("/api/templates")
async def list_templates(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    templates = await db_run(db_module.db_list_templates, phoneNumberId)
    return [
        {**template, "buttons": extract_template_buttons(template), "params": extract_template_params(template)}
        for template in templates
    ]


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    template = await db_run(db_module.db_get_template, template_id)
    if not template:
        raise HTTPException(404, "Modelo não encontrado")
    return {**template, "buttons": extract_template_buttons(template), "params": extract_template_params(template)}


def _create_template_tx(conn, doc: dict[str, Any]) -> dict[str, Any]:
    return db_module.db_create_template(conn, doc)


@app.post("/api/templates")
async def create_template(body: TemplateIn) -> dict[str, Any]:
    doc = {"id": new_id("tpl"), **body.model_dump(), "createdAt": now_iso()}
    async with STORE_MUTATION_LOCK:
        return await db_run(_create_template_tx, doc)


@app.post("/api/media")
async def upload_media(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    max_bytes = 120 * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(400, "Arquivo muito grande. Use até 120MB.")
    media_id = new_id("media")
    ext = Path(file.filename or "").suffix or ""
    media_dir = STORAGE_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / f"{media_id}{ext}"
    path.write_bytes(raw)
    doc = {
        "id": media_id,
        "filename": file.filename,
        "contentType": file.content_type,
        "size": len(raw),
        "path": str(path),
        "url": f"{public_base_url()}/api/media/{media_id}/raw" if public_base_url() else f"/api/media/{media_id}/raw",
        "createdAt": now_iso(),
    }
    async with STORE_MUTATION_LOCK:
        await db_run(db_module.db_insert_media, doc)
    return doc


@app.get("/api/media/{media_id}/raw")
async def media_raw(media_id: str):
    doc = await db_run(db_module.db_get_media, media_id)
    if not doc or not Path(doc["path"]).exists():
        raise HTTPException(404, "Mídia não encontrada")
    return FileResponse(doc["path"], media_type=doc.get("contentType"), filename=doc.get("filename"))


@app.get("/api/contacts")
async def list_contacts(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    rows = await db_run(db_module.db_list_contacts, phoneNumberId)
    return sorted(rows, key=lambda c: c.get("createdAt", ""), reverse=True)


def _get_contact_tx(conn, contact_id: str):
    contact = db_module.db_get_contact_by_id(conn, contact_id)
    if not contact:
        return None, None
    messages = db_module.db_get_messages_for_contact(conn, contact_id, limit=50)
    templates = db_module.db_list_templates(conn)
    return contact, [with_display_text(templates, m) for m in messages]


@app.get("/api/contacts/{contact_id}")
async def get_contact(contact_id: str) -> dict[str, Any]:
    contact, messages = await db_run(_get_contact_tx, contact_id)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    return {"contact": contact, "messages": messages}


def _update_contact_tx(conn, contact_id: str, body: ContactUpdateIn):
    return db_module.db_update_contact_full(
        conn, contact_id, body.name, sorted(set(body.tags or [])), sorted(set(body.lists or [])),
        body.customFields or {}, body.phoneNumberId, now_iso,
    )


@app.patch("/api/contacts/{contact_id}")
async def update_contact(contact_id: str, body: ContactUpdateIn) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        contact = await db_run(_update_contact_tx, contact_id, body)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    return contact


def _create_contact_tx(conn, body: LeadIn) -> dict[str, Any]:
    contact = upsert_contact(conn, body.phone, body.name, body.phoneNumberId)
    tag_ids = [ensure_named_tag(conn, value, body.phoneNumberId) for value in body.tags]
    list_ids = [ensure_named_list(conn, value, body.phoneNumberId) for value in body.lists]
    return attach_labels(conn, contact, [x for x in tag_ids if x], [x for x in list_ids if x], body.customFields)


@app.post("/api/contacts")
async def create_contact(body: LeadIn) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        return await db_run(_create_contact_tx, body)


def _import_contacts_tx(conn, rows: list[LeadIn]) -> int:
    count = 0
    for row in rows:
        contact = upsert_contact(conn, row.phone, row.name, row.phoneNumberId)
        tag_ids = [ensure_named_tag(conn, value, row.phoneNumberId) for value in row.tags]
        list_ids = [ensure_named_list(conn, value, row.phoneNumberId) for value in row.lists]
        attach_labels(conn, contact, [x for x in tag_ids if x], [x for x in list_ids if x], row.customFields)
        count += 1
    return count


@app.post("/api/contacts/import")
async def import_contacts(rows: list[LeadIn]) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        count = await db_run(_import_contacts_tx, rows)
    return {"count": count}


def _import_contacts_csv_tx(conn, rows: list[dict[str, str]], list_name: str, tags_str: str, phone_number_id: Optional[str]):
    list_id = ensure_named_list(conn, list_name, phone_number_id)
    tag_ids = [ensure_named_tag(conn, tag.strip(), phone_number_id) for tag in tags_str.split(",") if tag.strip()]
    imported = 0
    custom_fields: set[str] = set()
    for row in rows:
        phone = row.get("phone") or row.get("telefone") or row.get("whatsapp") or row.get("celular")
        if not phone:
            continue
        name = row.get("name") or row.get("nome")
        ignored = {"phone", "telefone", "whatsapp", "celular", "name", "nome", "tags", "listas", "lists"}
        custom = {k: v for k, v in row.items() if k and k.lower() not in ignored and v not in (None, "")}
        custom_fields.update(custom.keys())
        row_tags = [x.strip() for x in str(row.get("tags") or "").split(",") if x.strip()]
        row_lists = [x.strip() for x in str(row.get("lists") or row.get("listas") or "").split(",") if x.strip()]
        list_ids = [list_id] + [ensure_named_list(conn, x, phone_number_id) for x in row_lists]
        all_tag_ids = tag_ids + [ensure_named_tag(conn, x, phone_number_id) for x in row_tags]
        contact = upsert_contact(conn, phone, name, phone_number_id)
        attach_labels(conn, contact, all_tag_ids, [x for x in list_ids if x], custom)
        imported += 1
    return imported, list_id, custom_fields


@app.post("/api/contacts/import-csv")
async def import_contacts_csv(
    file: UploadFile = File(...),
    listName: Optional[str] = Form(None),
    tags: str = Form(""),
    phoneNumberId: Optional[str] = Form(None),
) -> dict[str, Any]:
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV sem cabeçalho")
    rows = list(reader)
    list_name = listName or Path(file.filename or "lista").stem
    async with STORE_MUTATION_LOCK:
        imported, list_id, custom_fields = await db_run(_import_contacts_csv_tx, rows, list_name, tags, phoneNumberId)
    return {"count": imported, "listId": list_id, "customFields": sorted(custom_fields)}


@app.get("/api/lists")
async def list_lists(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(db_module.db_list_lists, phoneNumberId)


def _create_list_tx(conn, name: str, phone_number_id: Optional[str]) -> dict[str, Any]:
    return db_module.db_create_list(conn, new_id("list"), name, phone_number_id, now_iso)


@app.post("/api/lists")
async def create_list(body: NamedIn) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        return await db_run(_create_list_tx, body.name, body.phoneNumberId)


@app.get("/api/custom-fields")
async def list_custom_fields(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(db_module.db_list_custom_fields, phoneNumberId)


def _create_custom_field_tx(conn, field_id: str, key: str, label: str, type_: str, phone_number_id: Optional[str]) -> dict[str, Any]:
    return db_module.db_upsert_custom_field(conn, field_id, key, label, type_, phone_number_id, now_iso)


@app.post("/api/custom-fields")
async def create_custom_field(body: CustomFieldIn) -> dict[str, Any]:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", body.key.strip()).strip("_")
    if not key:
        raise HTTPException(400, "Informe uma chave válida")
    field_id = f"{body.phoneNumberId or 'global'}_{key}"
    async with STORE_MUTATION_LOCK:
        return await db_run(_create_custom_field_tx, field_id, key, body.label or key, body.type, body.phoneNumberId)


@app.get("/api/tags")
async def list_tags(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(db_module.db_list_tags, phoneNumberId)


def _create_tag_tx(conn, tag_id: str, name: str, color: Optional[str], phone_number_id: Optional[str]) -> dict[str, Any]:
    return db_module.db_create_tag(conn, tag_id, name, color, phone_number_id, now_iso)


@app.post("/api/tags")
async def create_tag(body: NamedIn) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        return await db_run(_create_tag_tx, new_id("tag"), body.name, body.color, body.phoneNumberId)


def _inbox_tx(conn, phone_number_id: Optional[str]) -> list[dict[str, Any]]:
    conversations = db_module.db_list_conversations(conn, phone_number_id)
    latest_by_convo = db_module.db_get_latest_message_per_conversation(conn)
    templates = db_module.db_list_templates(conn)
    result = [{**c, "lastMessage": with_display_text(templates, latest_by_convo.get(c["id"]))} for c in conversations]
    return sorted(result, key=lambda c: c.get("lastMessageAt", ""), reverse=True)


@app.get("/api/inbox")
async def inbox(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(_inbox_tx, phoneNumberId)


def _conversation_detail_tx(conn, conversation_id: str):
    convo = db_module.db_get_conversation(conn, conversation_id)
    if not convo:
        return None
    db_module.db_mark_conversation_read(conn, conversation_id)
    convo = db_module.db_get_conversation(conn, conversation_id)
    contact = db_module.db_get_contact_by_phone(conn, convo["phone"])
    templates = db_module.db_list_templates(conn)
    messages = db_module.db_get_messages_for_conversation(conn, conversation_id)
    return convo, contact, [with_display_text(templates, m) for m in messages]


@app.get("/api/inbox/{conversation_id}")
async def conversation(conversation_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        result = await db_run(_conversation_detail_tx, conversation_id)
    if not result:
        raise HTTPException(404, "Conversa não encontrada")
    convo, contact, messages = result
    return {
        "conversation": convo,
        "contact": contact,
        "window": {
            "open": bool(convo.get("lastInboundAt") and datetime.fromisoformat(convo["lastInboundAt"]) > datetime.now(timezone.utc) - timedelta(hours=24)),
            "lastInboundAt": convo.get("lastInboundAt"),
        },
        "messages": messages,
    }


@app.post("/api/inbox/{conversation_id}/reply")
async def reply_conversation(conversation_id: str, body: SendMessageIn) -> dict[str, Any]:
    convo = await db_run(db_module.db_get_conversation, conversation_id)
    if not convo:
        raise HTTPException(404, "Conversa não encontrada")
    phone = body.phone or convo["phone"]
    channel_id = convo.get("phoneNumberId") or await active_phone_number_id()
    for item in body.items:
        if not item.phoneNumberId:
            item.phoneNumberId = channel_id
    results = await send_sequence(phone, body.items, source="inbox")
    return {"results": results}


@app.post("/api/messages/send")
async def send_message(body: SendMessageIn) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(400, "Adicione ao menos uma mensagem")
    results = await send_sequence(body.phone, body.items, source="manual")
    return {"sent": len([r for r in results if r["status"] == "sent"]), "results": results}


@app.get("/api/flows")
async def list_flows(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(db_module.db_list_flows, phoneNumberId)


@app.post("/api/flows")
async def create_flow(body: FlowIn) -> dict[str, Any]:
    doc = {"id": new_id("flow"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    async with STORE_MUTATION_LOCK:
        return await db_run(db_module.db_create_flow, doc)


@app.patch("/api/flows/{flow_id}")
async def update_flow(flow_id: str, body: FlowIn) -> dict[str, Any]:
    doc = {**body.model_dump(), "updatedAt": now_iso()}
    async with STORE_MUTATION_LOCK:
        flow = await db_run(db_module.db_update_flow, flow_id, doc)
    if not flow:
        raise HTTPException(404, "Fluxo não encontrado")
    return flow


@app.delete("/api/flows/{flow_id}")
async def delete_flow(flow_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        deleted = await db_run(db_module.db_delete_flow, flow_id)
    return {"deleted": deleted}


@app.get("/api/automations")
async def list_automations(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return await db_run(db_module.db_list_automations, phoneNumberId)


@app.post("/api/automations")
async def create_automation(body: AutomationIn) -> dict[str, Any]:
    doc = {"id": new_id("auto"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    async with STORE_MUTATION_LOCK:
        return await db_run(db_module.db_create_automation, doc)


@app.patch("/api/automations/{automation_id}")
async def update_automation(automation_id: str, body: AutomationIn) -> dict[str, Any]:
    doc = {**body.model_dump(), "updatedAt": now_iso()}
    async with STORE_MUTATION_LOCK:
        automation = await db_run(db_module.db_update_automation, automation_id, doc)
    if not automation:
        raise HTTPException(404, "Automação não encontrada")
    return automation


@app.delete("/api/automations/{automation_id}")
async def delete_automation(automation_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        deleted = await db_run(db_module.db_delete_automation, automation_id)
    return {"deleted": deleted}


@app.get("/api/campaigns")
async def list_campaigns(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    rows = await db_run(db_module.db_list_campaigns, phoneNumberId, True)
    return sorted(rows, key=lambda c: c.get("createdAt", ""), reverse=True)


def _estimate_campaign_tx(conn, body: TemplateCampaignIn) -> dict[str, Any]:
    included = contacts_for_campaign(conn, TemplateCampaignIn(**{**body.model_dump(), "exclusionListIds": []}))
    final = contacts_for_campaign(conn, body)
    excluded_ids = {c["id"] for c in included} - {c["id"] for c in final}
    return {
        "included": len(included),
        "excluded": len(excluded_ids),
        "receivers": len(final),
    }


@app.post("/api/campaigns/estimate")
async def estimate_campaign(body: TemplateCampaignIn) -> dict[str, Any]:
    return await db_run(_estimate_campaign_tx, body)


def _create_campaign_tx(conn, body: TemplateCampaignIn) -> dict[str, Any]:
    contacts = contacts_for_campaign(conn, body)
    status = "running" if body.sendNow else "scheduled" if body.scheduledAt else "draft"
    doc = {
        "id": new_id("camp"),
        "name": body.name,
        "templateName": body.templateName,
        "language": body.language,
        "phoneNumberId": body.phoneNumberId,
        "responseFlowId": body.responseFlowId,
        "scheduledAt": body.scheduledAt,
        "targetCount": len(contacts),
        "sent": 0,
        "failed": 0,
        "delivered": 0,
        "read": 0,
        "buttonClicks": 0,
        "lastError": None,
        "config": body.model_dump(),
        "status": status,
        "createdAt": now_iso(),
    }
    return db_module.db_create_campaign(conn, doc)


@app.post("/api/campaigns")
async def create_campaign(body: TemplateCampaignIn, background: BackgroundTasks) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        doc = await db_run(_create_campaign_tx, body)
    if body.sendNow:
        background.add_task(execute_campaign, doc["id"])
    return doc


@app.post("/api/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, background: BackgroundTasks) -> dict[str, Any]:
    # mark_campaign_resuming() (added by PR A / #2) already wraps its
    # read-modify-write cycle in STORE_MUTATION_LOCK internally, which is
    # exactly the fix PR B made here -- so calling it preserves both the
    # shared auto-resume/manual-resume code path from PR A and the
    # concurrency fix from PR B without duplicating either.
    campaign = await mark_campaign_resuming(campaign_id)
    background.add_task(execute_campaign, campaign_id, True)
    return campaign


def _update_campaign_tx(conn, campaign_id: str, body: CampaignUpdateIn):
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") in {"done", "running"} and not body.sendNow:
        raise HTTPException(status_code=400, detail="Somente campanhas agendadas, rascunhos ou falhas podem ser editadas")
    config = dict(campaign.get("config") or {})
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
        config["name"] = body.name
    if body.scheduledAt is not None:
        updates["scheduledAt"] = body.scheduledAt or None
        config["scheduledAt"] = body.scheduledAt or None
        config["sendNow"] = False
        updates["status"] = "scheduled" if body.scheduledAt else "draft"
    if body.batchSize is not None:
        config["batchSize"] = body.batchSize
    if body.batchPauseSeconds is not None:
        config["batchPauseSeconds"] = body.batchPauseSeconds
    updates["updatedAt"] = now_iso()
    resume_needed = False
    if body.sendNow:
        updates["scheduledAt"] = None
        config["scheduledAt"] = None
        config["sendNow"] = True
        updates["status"] = "running"
        updates["lastResumeAt"] = now_iso()
        updates["lastErrorText"] = None
        resume_needed = True
    updates["config"] = config
    db_module.db_update_campaign_fields(conn, campaign_id, updates)
    return db_module.db_get_campaign(conn, campaign_id, include_results=True), resume_needed


@app.patch("/api/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignUpdateIn, background: BackgroundTasks) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        campaign, resume_needed = await db_run(_update_campaign_tx, campaign_id, body)
    if resume_needed:
        background.add_task(execute_campaign, campaign_id, True)
    return campaign


def _cancel_campaign_tx(conn, campaign_id: str) -> dict[str, Any]:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=False)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    db_module.db_update_campaign_fields(conn, campaign_id, {"status": "canceled", "canceledAt": now_iso(), "lastErrorText": None})
    return db_module.db_get_campaign(conn, campaign_id, include_results=True)


@app.post("/api/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        return await db_run(_cancel_campaign_tx, campaign_id)


def _retry_failed_campaign_tx(conn, campaign_id: str) -> dict[str, Any]:
    campaign = db_module.db_get_campaign(conn, campaign_id, include_results=True)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    failed_rows = [row for row in campaign.get("results") or [] if row.get("status") == "failed"]
    if not failed_rows:
        raise HTTPException(status_code=400, detail="Nenhum lead com falha para reenviar")
    failed_keys = {(row.get("contactId"), normalize_phone(str(row.get("phone") or ""))) for row in failed_rows}
    db_module.db_delete_campaign_results_for_contacts(conn, campaign_id, failed_keys)
    db_module.db_update_campaign_fields(conn, campaign_id, {"status": "running", "lastRetryFailedAt": now_iso(), "lastErrorText": None})
    db_module.db_summarize_campaign(conn, campaign_id)
    return db_module.db_get_campaign(conn, campaign_id, include_results=True)


@app.post("/api/campaigns/{campaign_id}/retry-failed")
async def retry_failed_campaign(campaign_id: str, background: BackgroundTasks) -> dict[str, Any]:
    async with STORE_MUTATION_LOCK:
        campaign = await db_run(_retry_failed_campaign_tx, campaign_id)
    background.add_task(execute_campaign, campaign_id, True)
    return campaign
