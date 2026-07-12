import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
import csv
import io


APP_NAME = os.getenv("APP_NAME", "Simplific ONE API")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "change-me")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "/app/storage"))
STORE_PATH = STORAGE_DIR / "one-api.json"

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def start_scheduled_workers() -> None:
    await backfill_blacklist_from_button_clicks()
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


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        if not self.path.exists():
            self.path.write_text(json.dumps(self.empty(), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "contacts": [],
            "lists": [],
            "tags": [],
            "templates": [],
            "campaigns": [],
            "conversations": [],
            "messages": [],
            "automations": [],
            "automationRuns": [],
            "webhookEvents": [],
            "media": [],
            "flows": [],
            "phoneNumbers": [],
            "customFields": [],
            "settings": {},
        }

    def _read_sync(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = self.empty()
        merged = self.empty()
        merged.update(data)
        return merged

    async def read(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_sync)

    def _write_sync(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    async def write(self, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)


store = JsonStore(STORE_PATH)
STORE_MUTATION_LOCK = asyncio.Lock()


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


async def configured_meta() -> bool:
    data = await store.read()
    settings = data.get("settings", {}).get("meta", {})
    has_token = settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN")
    has_phone = (
        settings.get("phoneNumberId")
        or os.getenv("META_PHONE_NUMBER_ID")
        or next((p.get("phoneNumberId") or p.get("id") for p in data.get("phoneNumbers", []) if p.get("active")), "")
        or next(((p.get("phoneNumberId") or p.get("id")) for p in data.get("phoneNumbers", [])), "")
    )
    return bool(has_token and has_phone)


async def meta_config() -> dict[str, str]:
    data = await store.read()
    settings = data.get("settings", {}).get("meta", {})
    return {
        "accessToken": settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN") or "",
        "phoneNumberId": settings.get("phoneNumberId") or os.getenv("META_PHONE_NUMBER_ID") or "",
        "wabaId": settings.get("wabaId") or os.getenv("META_WABA_ID") or "",
        "appId": settings.get("appId") or os.getenv("META_APP_ID") or "",
        "appSecret": settings.get("appSecret") or os.getenv("META_APP_SECRET") or "",
        "businessName": settings.get("businessName") or "",
    }


async def active_phone_number_id(data: Optional[dict[str, Any]] = None, override: Optional[str] = None) -> str:
    if override:
        return override
    cfg = await meta_config()
    if cfg["phoneNumberId"]:
        return cfg["phoneNumberId"]
    data = data or await store.read()
    active = next((p for p in data.get("phoneNumbers", []) if p.get("active")), None)
    if active:
        return active.get("phoneNumberId") or active.get("id") or ""
    first = next(iter(data.get("phoneNumbers", [])), None)
    return (first or {}).get("phoneNumberId") or (first or {}).get("id") or ""


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


def mark_contact_scope(contact: dict[str, Any], phone_number_id: Optional[str]) -> None:
    scope = normalize_scope(phone_number_id)
    if not scope:
        return
    scopes = set(contact.get("phoneNumberIds") or [])
    scopes.add(scope)
    contact["phoneNumberIds"] = sorted(scopes)
    contact["lastPhoneNumberId"] = scope


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
    data: dict[str, Any],
    name: str,
    language: str,
    phone_number_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    matches = [
        t
        for t in data["templates"]
        if t.get("name") == name and (t.get("language") == language or not language)
    ]
    if phone_number_id:
        scoped_match = next((t for t in matches if scoped(t, phone_number_id)), None)
        if scoped_match:
            return scoped_match
    return next(iter(matches), None)


def template_preview_text(data: dict[str, Any], item: MessageItem | dict[str, Any]) -> str:
    payload = item.model_dump() if isinstance(item, MessageItem) else item
    name = payload.get("templateName") or payload.get("template", {}).get("name")
    language = payload.get("language") or (payload.get("template", {}).get("language") or {}).get("code") or ""
    phone_number_id = payload.get("phoneNumberId")
    if not name:
        return ""
    template = template_by_name(data, name, language, phone_number_id) or template_by_name(data, name, "", phone_number_id)
    text = (template or {}).get("bodyPreview") or name
    params = payload.get("templateParams") or {}
    for key, value in params.items():
        text = re.sub(r"\{\{\s*" + re.escape(str(key)) + r"\s*\}\}", str(value or ""), text)
    return text


def message_display_text(data: dict[str, Any], message: dict[str, Any]) -> str:
    if message.get("type") == "template":
        return template_preview_text(data, message.get("payload") or {}) or message.get("text") or message.get("type") or "-"
    return (
        message.get("text")
        or (message.get("payload") or {}).get("caption")
        or (message.get("payload") or {}).get("mediaUrl")
        or message.get("type")
        or "-"
    )


def with_display_text(data: dict[str, Any], message: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not message:
        return None
    return {**message, "displayText": message_display_text(data, message)}


def template_body_components(template_params: dict[str, Any]) -> list[dict[str, Any]]:
    if not template_params:
        return []
    ordered = [template_params[k] for k in sorted(template_params, key=lambda x: int(x) if str(x).isdigit() else str(x))]
    return [{
        "type": "body",
        "parameters": [{"type": "text", "text": str(value or "")} for value in ordered],
    }]


def upsert_contact(data: dict[str, Any], phone: str, name: Optional[str] = None, phone_number_id: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    if not normalized:
        raise HTTPException(400, "Telefone inválido")
    contact = next((c for c in data["contacts"] if c["phone"] == normalized), None)
    if contact:
        if name and not contact.get("name"):
            contact["name"] = name
        mark_contact_scope(contact, phone_number_id)
        contact.setdefault("customFields", {})
        contact["updatedAt"] = now_iso()
        return contact
    contact = {
        "id": new_id("lead"),
        "name": name,
        "phone": normalized,
        "lastPhoneNumberId": phone_number_id,
        "phoneNumberIds": [phone_number_id] if phone_number_id else [],
        "tags": [],
        "lists": [],
        "customFields": {},
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    data["contacts"].append(contact)
    return contact


def conversation_for(data: dict[str, Any], phone: str, name: Optional[str] = None, phone_number_id: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    convo = next((c for c in data["conversations"] if c["phone"] == normalized and (c.get("phoneNumberId") or "") == (phone_number_id or "")), None)
    if not convo and not phone_number_id:
        convo = next((c for c in data["conversations"] if c["phone"] == normalized), None)
    if convo:
        if phone_number_id and not convo.get("phoneNumberId"):
            convo["phoneNumberId"] = phone_number_id
        return convo
    convo = {
        "id": new_id("conv"),
        "phone": normalized,
        "phoneNumberId": phone_number_id,
        "name": name,
        "unread": 0,
        "lastMessageAt": now_iso(),
        "createdAt": now_iso(),
    }
    data["conversations"].append(convo)
    return convo


def attach_labels(contact: dict[str, Any], tags: list[str], lists: list[str], custom_fields: Optional[dict[str, Any]] = None) -> None:
    contact["tags"] = sorted(set(contact.get("tags") or []) | set(tags or []))
    contact["lists"] = sorted(set(contact.get("lists") or []) | set(lists or []))
    if custom_fields:
        contact["customFields"] = {**(contact.get("customFields") or {}), **custom_fields}
    contact["updatedAt"] = now_iso()


def resolve_tag_ids(data: dict[str, Any], values: list[str], phone_number_id: Optional[str] = None) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        if not value:
            continue
        text = str(value).strip()
        existing = next((tag for tag in data["tags"] if tag.get("id") == text), None)
        ids.append(existing["id"] if existing else ensure_named_tag(data, text, phone_number_id))
    return [item for item in ids if item]


def resolve_list_ids(data: dict[str, Any], values: list[str], phone_number_id: Optional[str] = None) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        if not value:
            continue
        text = str(value).strip()
        existing = next((row for row in data["lists"] if row.get("id") == text), None)
        ids.append(existing["id"] if existing else ensure_named_list(data, text, phone_number_id))
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


async def send_sequence(phone: str, items: list[MessageItem], source: str = "manual") -> list[dict[str, Any]]:
    initial_data = await store.read()
    sequence_phone_number_id = next((item.phoneNumberId for item in items if item.phoneNumberId), None) or await active_phone_number_id(initial_data)
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
        data = await store.read()
        contact = upsert_contact(data, phone, phone_number_id=sequence_phone_number_id)
        convo = conversation_for(data, phone, contact.get("name"), sequence_phone_number_id)
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
                "text": template_preview_text(data, item) if item.type == "template" else (item.text or item.caption or item.mediaUrl),
                "payload": item.model_dump(),
                "status": status,
                "source": source,
                "providerMessageId": meta_message_id(response),
                "providerResponse": response,
                "error": error,
                "errorText": readable_error(error),
                "createdAt": created_at,
            }
            data["messages"].append(msg)
            results.append(msg)
        convo["lastMessageAt"] = now_iso()
        await store.write(data)
        return results


async def set_pending_response_flow(phone: str, response_flow_id: Optional[str] = None, button_flow_map: Optional[dict[str, str]] = None, campaign_id: Optional[str] = None, phone_number_id: Optional[str] = None) -> None:
    async with STORE_MUTATION_LOCK:
        data = await store.read()
        contact = upsert_contact(data, phone, phone_number_id=phone_number_id)
        if button_flow_map:
            contact["pendingResponseFlows"] = button_flow_map
        elif response_flow_id:
            contact["pendingResponseFlowId"] = response_flow_id
        if campaign_id:
            contact["pendingCampaignId"] = campaign_id
        await store.write(data)


def ensure_named_list(data: dict[str, Any], name: str, phone_number_id: Optional[str] = None) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    scope = normalize_scope(phone_number_id)
    existing = next((x for x in data["lists"] if x.get("name", "").lower() == clean.lower() and (x.get("phoneNumberId") or "") == scope), None)
    if existing:
        return existing["id"]
    doc = {"id": new_id("list"), "name": clean, "phoneNumberId": scope or None, "createdAt": now_iso()}
    data["lists"].append(doc)
    return doc["id"]


def ensure_named_tag(data: dict[str, Any], name: str, phone_number_id: Optional[str] = None) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    scope = normalize_scope(phone_number_id)
    existing = next((x for x in data["tags"] if x.get("name", "").lower() == clean.lower() and (x.get("phoneNumberId") or "") == scope), None)
    if existing:
        return existing["id"]
    doc = {"id": new_id("tag"), "name": clean, "color": "#84ff00", "phoneNumberId": scope or None, "createdAt": now_iso()}
    data["tags"].append(doc)
    return doc["id"]


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


async def backfill_blacklist_from_button_clicks() -> None:
    data = await store.read()
    changed = 0
    for message in data.get("messages", []):
        if message.get("direction") != "in" or not is_blacklist_button_text(inbound_button_text(message)):
            continue
        contact = next(
            (
                row
                for row in data["contacts"]
                if row.get("id") == message.get("contactId")
                or row.get("phone") == normalize_phone(str(message.get("phone") or ""))
            ),
            None,
        )
        if not contact:
            continue
        channel_id = message.get("phoneNumberId") or contact.get("lastPhoneNumberId")
        blacklist_id = ensure_named_list(data, "Blacklist", channel_id)
        before = set(contact.get("lists") or [])
        attach_labels(contact, [], [blacklist_id])
        if set(contact.get("lists") or []) != before:
            changed += 1
    if changed:
        await store.write(data)
        print(f"Applied Blacklist backfill to {changed} contacts")


async def run_flow_for_contact(phone: str, flow_id: str, source: str = "flow") -> dict[str, Any]:
    data = await store.read()
    flow = next((f for f in data["flows"] if f["id"] == flow_id and f.get("enabled", True)), None)
    if not flow:
        return {"skipped": True, "reason": "flow_not_found_or_disabled"}
    flow_phone_number_id = flow.get("phoneNumberId")
    contact = upsert_contact(data, phone, phone_number_id=flow_phone_number_id)
    flow_phone_number_id = flow_phone_number_id or contact.get("lastPhoneNumberId") or await active_phone_number_id(data)
    sent_items: list[MessageItem] = []
    for action in flow.get("actions") or []:
        action_type = action.get("type")
        if action_type == "delay":
            await asyncio.sleep(int(action.get("delaySeconds") or 0))
        elif action_type == "add_tags":
            attach_labels(contact, resolve_tag_ids(data, action.get("tags") or [], flow_phone_number_id), [])
        elif action_type == "add_lists":
            attach_labels(contact, [], resolve_list_ids(data, action.get("lists") or [], flow_phone_number_id))
        elif action_type == "send_message":
            sent_items.append(MessageItem(type="text", text=action.get("text"), phoneNumberId=flow_phone_number_id, delaySeconds=int(action.get("delaySeconds") or 0)))
        elif action_type in {"image", "video", "audio", "document"}:
            sent_items.append(MessageItem(type=action_type, mediaUrl=action.get("mediaUrl"), caption=action.get("caption"), phoneNumberId=flow_phone_number_id, delaySeconds=int(action.get("delaySeconds") or 0)))
    await store.write(data)
    results = await send_sequence(phone, sent_items, source=source) if sent_items else []
    return {"flowId": flow_id, "messages": len(results)}


def contacts_for_campaign(data: dict[str, Any], body: TemplateCampaignIn) -> list[dict[str, Any]]:
    selected = []
    excluded = set(body.exclusionListIds or [])
    for contact in data["contacts"]:
        if body.phoneNumberId and not scoped(contact, body.phoneNumberId):
            continue
        contact_lists = set(contact.get("lists") or [])
        contact_tags = set(contact.get("tags") or [])
        in_list = not body.listIds or bool(contact_lists & set(body.listIds))
        in_tag = not body.tagIds or bool(contact_tags & set(body.tagIds))
        is_excluded = bool(contact_lists & excluded)
        if in_list and in_tag and not is_excluded:
            selected.append(contact)
    return selected


def campaign_processed_keys(campaign: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in campaign.get("results") or []:
        if row.get("contactId"):
            keys.add(("id", str(row["contactId"])))
        if row.get("phone"):
            keys.add(("phone", normalize_phone(str(row["phone"]))))
    return keys


def summarize_campaign(campaign: dict[str, Any]) -> None:
    results = campaign.get("results") or []
    campaign["sent"] = sum(1 for row in results if row.get("status") == "sent")
    campaign["failed"] = sum(1 for row in results if row.get("status") == "failed")
    campaign["delivered"] = sum(1 for row in results if row.get("deliveredAt"))
    campaign["read"] = sum(1 for row in results if row.get("readAt"))
    campaign["buttonClicks"] = sum(1 for row in results if row.get("clickedAt"))
    campaign["lastError"] = next((row.get("error") for row in reversed(results) if row.get("error")), None)
    campaign["lastErrorText"] = next((row.get("errorText") for row in reversed(results) if row.get("errorText")), None)


async def persist_campaign_result(campaign_id: str, row: dict[str, Any], target_count: int) -> None:
    async with STORE_MUTATION_LOCK:
        data = await store.read()
        campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
        if not campaign:
            return
        campaign.setdefault("results", []).append(row)
        campaign["targetCount"] = target_count
        campaign["status"] = "running"
        campaign["lastProgressAt"] = now_iso()
        summarize_campaign(campaign)
        await store.write(data)


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


async def persist_campaign_batch(campaign_id: str, body: TemplateCampaignIn, results: list[dict[str, Any]], target_count: int) -> None:
    async with STORE_MUTATION_LOCK:
        data = await store.read()
        campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
        if not campaign:
            return
        for result in results:
            contact = result["contact"]
            item = result["item"]
            phone_number_id = item.phoneNumberId or body.phoneNumberId or await active_phone_number_id(data)
            stored_contact = upsert_contact(data, contact["phone"], contact.get("name"), phone_number_id)
            convo = conversation_for(data, contact["phone"], stored_contact.get("name"), phone_number_id)
            msg = {
                "id": new_id("msg"),
                "conversationId": convo["id"],
                "contactId": stored_contact["id"],
                "phone": stored_contact["phone"],
                "phoneNumberId": phone_number_id,
                "direction": "out",
                "type": item.type,
                "text": template_preview_text(data, item),
                "payload": item.model_dump(),
                "status": result["status"],
                "source": f"campaign:{campaign_id}",
                "providerMessageId": meta_message_id(result["response"]),
                "providerResponse": result["response"],
                "error": result["error"],
                "errorText": result["errorText"],
                "createdAt": result["createdAt"],
            }
            data["messages"].append(msg)
            convo["lastMessageAt"] = now_iso()
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
            campaign.setdefault("results", []).append(row)
            if body.buttonFlowMap:
                stored_contact["pendingResponseFlows"] = body.buttonFlowMap
            elif body.responseFlowId:
                stored_contact["pendingResponseFlowId"] = body.responseFlowId
            stored_contact["pendingCampaignId"] = campaign_id
        campaign["targetCount"] = target_count
        campaign["status"] = "running"
        campaign["lastProgressAt"] = now_iso()
        summarize_campaign(campaign)
        await store.write(data)


async def execute_campaign(campaign_id: str, resume: bool = False) -> None:
    data = await store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        return
    campaign["status"] = "running"
    campaign["startedAt"] = campaign.get("startedAt") or now_iso()
    campaign["lastProgressAt"] = now_iso()
    campaign.setdefault("results", [])
    summarize_campaign(campaign)
    await store.write(data)

    body = TemplateCampaignIn(**campaign["config"])
    contacts = contacts_for_campaign(data, body)
    processed = campaign_processed_keys(campaign) if resume else set()
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
            current = next((c for c in (await store.read())["campaigns"] if c["id"] == campaign_id), None)
            if current and current.get("status") == "canceled":
                return
            batch = pending_contacts[start:start + batch_size]
            batch_results = await asyncio.gather(*(execute_campaign_contact(campaign_id, body, contact) for contact in batch))
            await persist_campaign_batch(campaign_id, body, batch_results, len(contacts))
            if batch_pause and start + batch_size < len(pending_contacts):
                await asyncio.sleep(batch_pause)
    except Exception as exc:
        async with STORE_MUTATION_LOCK:
            data = await store.read()
            campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
            if campaign:
                campaign["status"] = "failed"
                campaign["lastErrorText"] = str(exc)
                campaign["failedAt"] = now_iso()
                summarize_campaign(campaign)
                await store.write(data)
        return

    async with STORE_MUTATION_LOCK:
        data = await store.read()
        campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
        if campaign:
            campaign["status"] = "done"
            campaign["targetCount"] = len(contacts)
            campaign["finishedAt"] = now_iso()
            summarize_campaign(campaign)
        await store.write(data)


async def scheduled_campaign_loop() -> None:
    while True:
        try:
            data = await store.read()
            now = datetime.now(timezone.utc)
            due = [
                c["id"]
                for c in data["campaigns"]
                if c.get("status") == "scheduled" and parse_datetime(c.get("scheduledAt")) and parse_datetime(c.get("scheduledAt")) <= now
            ]
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


async def run_matching_automations(phone: str, inbound: dict[str, Any]) -> None:
    data = await store.read()
    channel_id = inbound.get("phoneNumberId")
    contact = upsert_contact(data, phone, inbound.get("name"), channel_id)
    button_text = str(inbound.get("buttonText") or inbound.get("text") or "").strip()
    pending_map = contact.get("pendingResponseFlows") or {}
    pending_flow = pending_map.get(button_text) or contact.get("pendingResponseFlowId")
    if pending_flow:
        contact["pendingResponseFlowId"] = None
        if pending_map:
            contact["pendingResponseFlows"] = {}
        await store.write(data)
        await run_flow_for_contact(contact["phone"], pending_flow, source=f"button-flow:{pending_flow}")
        data = await store.read()
        contact = upsert_contact(data, phone, inbound.get("name"), channel_id)
    matches = [a for a in data["automations"] if scoped(a, channel_id) and automation_matches(a, inbound)]
    for automation in matches:
        attach_labels(
            contact,
            resolve_tag_ids(data, automation.get("addTags") or [], channel_id),
            resolve_list_ids(data, automation.get("addLists") or [], channel_id),
        )
        data["automationRuns"].append({
            "id": new_id("run"),
            "automationId": automation["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "trigger": inbound,
            "createdAt": now_iso(),
        })
        await store.write(data)
        items = [MessageItem(**item) for item in automation.get("items") or []]
        if items:
            await send_sequence(contact["phone"], items, source=f"automation:{automation['id']}")
        data = await store.read()


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


def update_campaign_delivery_from_status(data: dict[str, Any], status: dict[str, Any]) -> None:
    provider_id = status.get("providerMessageId")
    status_name = status.get("status")
    if not provider_id or status_name not in {"delivered", "read", "failed", "sent"}:
        return
    timestamp_field = {
        "delivered": "deliveredAt",
        "read": "readAt",
        "sent": "sentAt",
    }.get(status_name)
    for msg in data["messages"]:
        if msg.get("providerMessageId") == provider_id:
            msg["deliveryStatus"] = status_name
            msg["deliveryPayload"] = status.get("payload")
            msg["deliveryUpdatedAt"] = status["createdAt"]
            if status_name == "failed":
                msg["status"] = "failed"
                msg["error"] = (status.get("payload") or {}).get("errors")
                msg["errorText"] = readable_error(msg["error"])
            break
    for campaign in data["campaigns"]:
        changed = False
        for row in campaign.get("results") or []:
            if provider_id in (row.get("providerMessageIds") or []):
                if timestamp_field:
                    row[timestamp_field] = row.get(timestamp_field) or status["createdAt"]
                if status_name == "failed":
                    row["status"] = "failed"
                    row["error"] = (status.get("payload") or {}).get("errors") or row.get("error")
                    row["errorText"] = readable_error(row.get("error")) or row.get("errorText")
                changed = True
        if changed:
            campaign["delivered"] = sum(1 for row in campaign.get("results") or [] if row.get("deliveredAt") or row.get("readAt"))
            campaign["read"] = sum(1 for row in campaign.get("results") or [] if row.get("readAt"))
            campaign["failed"] = sum(1 for row in campaign.get("results") or [] if row.get("status") == "failed")
            campaign["sent"] = sum(1 for row in campaign.get("results") or [] if row.get("status") == "sent")
            campaign["lastError"] = next((row.get("error") for row in reversed(campaign.get("results") or []) if row.get("error")), None)
            campaign["lastErrorText"] = next((row.get("errorText") for row in reversed(campaign.get("results") or []) if row.get("errorText")), None)
            campaign["lastStatusAt"] = status["createdAt"]


def update_campaign_click_from_inbound(data: dict[str, Any], inbound: dict[str, Any]) -> None:
    button_text = inbound.get("buttonText")
    if not button_text:
        return
    phone = normalize_phone(inbound.get("phone") or "")
    contact = next((c for c in data["contacts"] if c.get("phone") == phone), None)
    campaign_id = contact.get("pendingCampaignId") if contact else None
    if not campaign_id:
        return
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        return
    for row in campaign.get("results") or []:
        if row.get("phone") == phone:
            row["clickedAt"] = row.get("clickedAt") or inbound["createdAt"]
            row["buttonText"] = button_text
            break
    campaign["buttonClicks"] = sum(1 for row in campaign.get("results") or [] if row.get("clickedAt"))
    campaign["lastClickAt"] = inbound["createdAt"]


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


@app.get("/api/dashboard")
async def dashboard(phoneNumberId: Optional[str] = None) -> dict[str, Any]:
    data = await store.read()
    contacts = [c for c in data["contacts"] if scoped(c, phoneNumberId)]
    lists = [row for row in data["lists"] if scoped(row, phoneNumberId)]
    tags = [row for row in data["tags"] if scoped(row, phoneNumberId)]
    templates = [row for row in data["templates"] if scoped(row, phoneNumberId)]
    campaigns = [row for row in data["campaigns"] if scoped(row, phoneNumberId)]
    conversations = [row for row in data["conversations"] if scoped(row, phoneNumberId)]
    messages = [row for row in data["messages"] if scoped(row, phoneNumberId)]
    automations = [row for row in data["automationRuns"] if scoped(row, phoneNumberId)]
    unread = sum(1 for c in conversations if c.get("unread", 0) > 0)
    sent = sum(1 for m in messages if m.get("direction") == "out" and m.get("status") == "sent")
    failed = sum(1 for m in messages if m.get("direction") == "out" and m.get("status") == "failed")
    return {
        "contacts": len(contacts),
        "lists": len(lists),
        "tags": len(tags),
        "templates": len(templates),
        "campaigns": len(campaigns),
        "inboxUnread": unread,
        "automationRuns": len(automations),
        "messagesSent": sent,
        "messagesFailed": failed,
    }


@app.get("/api/meta/webhooks/recent")
async def recent_webhooks(limit: int = 25) -> dict[str, Any]:
    data = await store.read()
    events = list(reversed(data.get("webhookEvents", [])))[:max(1, min(limit, 100))]
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


@app.post("/api/meta/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    payload = await request.json()
    data = await store.read()
    data["webhookEvents"].append({"id": new_id("evt"), "payload": payload, "createdAt": now_iso()})
    inbound_messages = extract_webhook_messages(payload)
    delivery_statuses = extract_webhook_statuses(payload)
    for status in delivery_statuses:
        update_campaign_delivery_from_status(data, status)
    for inbound in inbound_messages:
        channel_id = inbound.get("phoneNumberId")
        contact = upsert_contact(data, inbound["phone"], inbound.get("name"), channel_id)
        convo = conversation_for(data, contact["phone"], contact.get("name"), channel_id)
        if channel_id:
            convo["phoneNumberId"] = channel_id
            convo["displayPhoneNumber"] = inbound.get("displayPhoneNumber")
        convo["unread"] = int(convo.get("unread") or 0) + 1
        convo["lastMessageAt"] = inbound["createdAt"]
        convo["lastInboundAt"] = inbound["createdAt"]
        data["messages"].append({
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
        update_campaign_click_from_inbound(data, inbound)
    await store.write(data)
    for inbound in inbound_messages:
        background.add_task(run_matching_automations, inbound["phone"], inbound)
    return {"received": True, "time": now_iso(), "messages": len(inbound_messages), "statuses": len(delivery_statuses)}


@app.get("/api/settings")
async def settings() -> dict[str, Any]:
    data = await store.read()
    meta = {**await meta_config()}
    if meta.get("accessToken"):
        meta["accessTokenPreview"] = f"{meta['accessToken'][:8]}...{meta['accessToken'][-4:]}" if len(meta["accessToken"]) > 14 else "***"
        meta["accessToken"] = ""
    return {"metaGraphVersion": META_GRAPH_VERSION, "meta": meta, **data.get("settings", {})}


@app.post("/api/meta/settings")
async def save_meta_settings(body: MetaSettingsIn) -> dict[str, Any]:
    data = await store.read()
    current = data.setdefault("settings", {}).setdefault("meta", {})
    update = body.model_dump()
    if not update.get("accessToken"):
        update.pop("accessToken", None)
    current.update({k: v for k, v in update.items() if v not in (None, "")})
    current["updatedAt"] = now_iso()
    await store.write(data)
    return {"saved": True, "metaConfigured": await configured_meta()}


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
    data = await store.read()
    synced = []
    channel_id = await active_phone_number_id(data)
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
        existing = next((t for t in data["templates"] if t["id"] == doc["id"]), None)
        if existing:
            existing.update(doc)
        else:
            data["templates"].append(doc)
        synced.append(doc)
    await store.write(data)
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


@app.post("/api/meta/subscribe-webhook")
async def subscribe_webhook() -> dict[str, Any]:
    cfg = await meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para ativar o webhook.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/subscribed_apps"
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    response_payload: Any = res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text
    data = await store.read()
    meta = data.setdefault("settings", {}).setdefault("meta", {})
    if res.status_code >= 400:
        meta["webhookSubscribed"] = False
        meta["lastWebhookSubscribeError"] = response_payload
        meta["lastWebhookSubscribeErrorText"] = readable_error(response_payload) or str(response_payload)
        meta["lastWebhookSubscribeErrorAt"] = now_iso()
        await store.write(data)
        raise HTTPException(res.status_code, response_payload)
    meta["webhookSubscribed"] = True
    meta["webhookSubscribedAt"] = now_iso()
    meta["lastWebhookSubscribeError"] = None
    meta["lastWebhookSubscribeErrorText"] = None
    await store.write(data)
    return {"subscribed": True, "response": response_payload}


@app.get("/api/phone-numbers")
async def list_phone_numbers() -> list[dict[str, Any]]:
    data = await store.read()
    numbers = data["phoneNumbers"]
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


@app.post("/api/phone-numbers")
async def add_phone_number(body: PhoneNumberManualIn) -> dict[str, Any]:
    data = await store.read()
    doc = {
        "id": body.phoneNumberId,
        "phoneNumberId": body.phoneNumberId,
        "displayPhoneNumber": body.displayPhoneNumber,
        "verifiedName": body.verifiedName,
        "qualityRating": body.qualityRating or "UNKNOWN",
        "messagingLimitTier": body.messagingLimitTier or "UNKNOWN",
        "active": not data["phoneNumbers"],
        "source": "manual",
        "createdAt": now_iso(),
    }
    existing = next((p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) == body.phoneNumberId), None)
    if existing:
        existing.update(doc)
    else:
        data["phoneNumbers"].append(doc)
    await store.write(data)
    return doc


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
    data = await store.read()
    synced = []
    active_id = await active_phone_number_id(data)
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
        existing = next((p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) == row.get("id")), None)
        if existing:
            existing.update(doc)
        else:
            data["phoneNumbers"].append(doc)
        synced.append(doc)
    await store.write(data)
    return {"count": len(synced), "phoneNumbers": synced}


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
    data = await store.read()
    existing = next((p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) == phone_number_id), None)
    if not existing:
        existing = {
            "id": phone_number_id,
            "phoneNumberId": phone_number_id,
            "active": data.get("settings", {}).get("meta", {}).get("phoneNumberId") == phone_number_id,
            "source": "meta",
            "createdAt": now_iso(),
        }
        data["phoneNumbers"].append(existing)
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
    await store.write(data)
    return existing


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

    data = await store.read()
    existing = next((p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) == phone_number_id), None)
    if not existing:
        existing = {
            "id": phone_number_id,
            "phoneNumberId": phone_number_id,
            "active": data.get("settings", {}).get("meta", {}).get("phoneNumberId") == phone_number_id,
            "source": "meta",
            "createdAt": now_iso(),
        }
        data["phoneNumbers"].append(existing)

    if res.status_code >= 400:
        existing.update({
            "registrationStatus": "failed",
            "registered": False,
            "lastRegistrationError": response_payload,
            "lastRegistrationErrorText": readable_error(response_payload) or str(response_payload),
            "lastRegistrationErrorAt": now_iso(),
        })
        await store.write(data)
        raise HTTPException(res.status_code, response_payload)

    existing.update({
        "registrationStatus": "registered",
        "registered": True,
        "registeredAt": now_iso(),
        "lastRegistrationResponse": response_payload,
        "lastRegistrationError": None,
        "lastRegistrationErrorText": None,
    })
    await store.write(data)
    return {"registered": True, "phoneNumber": existing, "response": response_payload}


@app.post("/api/phone-numbers/{phone_number_id}/activate")
async def activate_phone_number(phone_number_id: str) -> dict[str, Any]:
    data = await store.read()
    found = False
    for phone in data["phoneNumbers"]:
        is_active = (phone.get("phoneNumberId") or phone.get("id")) == phone_number_id
        phone["active"] = is_active
        found = found or is_active
    if not found:
        raise HTTPException(404, "Número não encontrado")
    data.setdefault("settings", {}).setdefault("meta", {})["phoneNumberId"] = phone_number_id
    await store.write(data)
    return {"active": phone_number_id}


@app.delete("/api/phone-numbers/{phone_number_id}")
async def delete_phone_number(phone_number_id: str) -> dict[str, Any]:
    data = await store.read()
    before = len(data["phoneNumbers"])
    data["phoneNumbers"] = [p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) != phone_number_id]
    if data.get("settings", {}).get("meta", {}).get("phoneNumberId") == phone_number_id:
        data["settings"]["meta"].pop("phoneNumberId", None)
    await store.write(data)
    return {"deleted": before - len(data["phoneNumbers"])}


@app.get("/api/templates")
async def list_templates(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    data = await store.read()
    rows = []
    for template in data["templates"]:
        if not scoped(template, phoneNumberId):
            continue
        rows.append({
            **template,
            "buttons": extract_template_buttons(template),
            "params": extract_template_params(template),
        })
    return rows


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    data = await store.read()
    template = next((t for t in data["templates"] if t["id"] == template_id), None)
    if not template:
        raise HTTPException(404, "Modelo não encontrado")
    return {**template, "buttons": extract_template_buttons(template), "params": extract_template_params(template)}


@app.post("/api/templates")
async def create_template(body: TemplateIn) -> dict[str, Any]:
    data = await store.read()
    doc = {"id": new_id("tpl"), **body.model_dump(), "createdAt": now_iso()}
    data["templates"].append(doc)
    await store.write(data)
    return doc


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
    data = await store.read()
    doc = {
        "id": media_id,
        "filename": file.filename,
        "contentType": file.content_type,
        "size": len(raw),
        "path": str(path),
        "url": f"{public_base_url()}/api/media/{media_id}/raw" if public_base_url() else f"/api/media/{media_id}/raw",
        "createdAt": now_iso(),
    }
    data["media"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/media/{media_id}/raw")
async def media_raw(media_id: str):
    data = await store.read()
    doc = next((m for m in data["media"] if m["id"] == media_id), None)
    if not doc or not Path(doc["path"]).exists():
        raise HTTPException(404, "Mídia não encontrada")
    return FileResponse(doc["path"], media_type=doc.get("contentType"), filename=doc.get("filename"))


@app.get("/api/contacts")
async def list_contacts(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    rows = [row for row in (await store.read())["contacts"] if scoped(row, phoneNumberId)]
    return sorted(rows, key=lambda c: c.get("createdAt", ""), reverse=True)


@app.get("/api/contacts/{contact_id}")
async def get_contact(contact_id: str) -> dict[str, Any]:
    data = await store.read()
    contact = next((c for c in data["contacts"] if c["id"] == contact_id), None)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    messages = [m for m in data["messages"] if m.get("contactId") == contact_id]
    return {"contact": contact, "messages": [with_display_text(data, m) for m in messages[-50:]]}


@app.patch("/api/contacts/{contact_id}")
async def update_contact(contact_id: str, body: ContactUpdateIn) -> dict[str, Any]:
    data = await store.read()
    contact = next((c for c in data["contacts"] if c["id"] == contact_id), None)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    contact["name"] = body.name
    contact["tags"] = sorted(set(body.tags or []))
    contact["lists"] = sorted(set(body.lists or []))
    contact["customFields"] = body.customFields or {}
    mark_contact_scope(contact, body.phoneNumberId)
    contact["updatedAt"] = now_iso()
    await store.write(data)
    return contact


@app.post("/api/contacts")
async def create_contact(body: LeadIn) -> dict[str, Any]:
    data = await store.read()
    contact = upsert_contact(data, body.phone, body.name, body.phoneNumberId)
    tag_ids = [ensure_named_tag(data, value, body.phoneNumberId) for value in body.tags]
    list_ids = [ensure_named_list(data, value, body.phoneNumberId) for value in body.lists]
    attach_labels(contact, [x for x in tag_ids if x], [x for x in list_ids if x], body.customFields)
    await store.write(data)
    return contact


@app.post("/api/contacts/import")
async def import_contacts(rows: list[LeadIn]) -> dict[str, Any]:
    data = await store.read()
    count = 0
    for row in rows:
        contact = upsert_contact(data, row.phone, row.name, row.phoneNumberId)
        tag_ids = [ensure_named_tag(data, value, row.phoneNumberId) for value in row.tags]
        list_ids = [ensure_named_list(data, value, row.phoneNumberId) for value in row.lists]
        attach_labels(contact, [x for x in tag_ids if x], [x for x in list_ids if x], row.customFields)
        count += 1
    await store.write(data)
    return {"count": count}


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
    data = await store.read()
    list_id = ensure_named_list(data, listName or Path(file.filename or "lista").stem, phoneNumberId)
    tag_ids = [ensure_named_tag(data, tag.strip(), phoneNumberId) for tag in tags.split(",") if tag.strip()]
    imported = 0
    custom_fields = set()
    for row in reader:
        phone = row.get("phone") or row.get("telefone") or row.get("whatsapp") or row.get("celular")
        if not phone:
            continue
        name = row.get("name") or row.get("nome")
        ignored = {"phone", "telefone", "whatsapp", "celular", "name", "nome", "tags", "listas", "lists"}
        custom = {k: v for k, v in row.items() if k and k.lower() not in ignored and v not in (None, "")}
        custom_fields.update(custom.keys())
        row_tags = [x.strip() for x in str(row.get("tags") or "").split(",") if x.strip()]
        row_lists = [x.strip() for x in str(row.get("lists") or row.get("listas") or "").split(",") if x.strip()]
        list_ids = [list_id] + [ensure_named_list(data, x, phoneNumberId) for x in row_lists]
        all_tag_ids = tag_ids + [ensure_named_tag(data, x, phoneNumberId) for x in row_tags]
        contact = upsert_contact(data, phone, name, phoneNumberId)
        attach_labels(contact, all_tag_ids, [x for x in list_ids if x], custom)
        imported += 1
    await store.write(data)
    return {"count": imported, "listId": list_id, "customFields": sorted(custom_fields)}


@app.get("/api/lists")
async def list_lists(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return [row for row in (await store.read())["lists"] if scoped(row, phoneNumberId)]


@app.post("/api/lists")
async def create_list(body: NamedIn) -> dict[str, Any]:
    data = await store.read()
    doc = {"id": new_id("list"), "name": body.name, "phoneNumberId": body.phoneNumberId, "createdAt": now_iso()}
    data["lists"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/custom-fields")
async def list_custom_fields(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return [row for row in (await store.read())["customFields"] if scoped(row, phoneNumberId)]


@app.post("/api/custom-fields")
async def create_custom_field(body: CustomFieldIn) -> dict[str, Any]:
    data = await store.read()
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", body.key.strip()).strip("_")
    if not key:
        raise HTTPException(400, "Informe uma chave válida")
    doc = {"id": f"{body.phoneNumberId or 'global'}_{key}", "key": key, "label": body.label or key, "type": body.type, "phoneNumberId": body.phoneNumberId, "createdAt": now_iso()}
    existing = next((f for f in data["customFields"] if f["key"] == key and (f.get("phoneNumberId") or "") == (body.phoneNumberId or "")), None)
    if existing:
        existing.update(doc)
    else:
        data["customFields"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/tags")
async def list_tags(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return [row for row in (await store.read())["tags"] if scoped(row, phoneNumberId)]


@app.post("/api/tags")
async def create_tag(body: NamedIn) -> dict[str, Any]:
    data = await store.read()
    doc = {"id": new_id("tag"), "name": body.name, "color": body.color or "#84ff00", "phoneNumberId": body.phoneNumberId, "createdAt": now_iso()}
    data["tags"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/inbox")
async def inbox(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    data = await store.read()
    latest_by_convo = {}
    for msg in data["messages"]:
        latest_by_convo[msg["conversationId"]] = msg
    conversations = []
    for convo in data["conversations"]:
        if not scoped(convo, phoneNumberId):
            continue
        conversations.append({**convo, "lastMessage": with_display_text(data, latest_by_convo.get(convo["id"]))})
    return sorted(conversations, key=lambda c: c.get("lastMessageAt", ""), reverse=True)


@app.get("/api/inbox/{conversation_id}")
async def conversation(conversation_id: str) -> dict[str, Any]:
    data = await store.read()
    convo = next((c for c in data["conversations"] if c["id"] == conversation_id), None)
    if not convo:
        raise HTTPException(404, "Conversa não encontrada")
    convo["unread"] = 0
    await store.write(data)
    return {
        "conversation": convo,
        "contact": next((c for c in data["contacts"] if c["phone"] == convo["phone"]), None),
        "window": {
            "open": bool(convo.get("lastInboundAt") and datetime.fromisoformat(convo["lastInboundAt"]) > datetime.now(timezone.utc) - timedelta(hours=24)),
            "lastInboundAt": convo.get("lastInboundAt"),
        },
        "messages": [with_display_text(data, m) for m in data["messages"] if m["conversationId"] == conversation_id],
    }


@app.post("/api/inbox/{conversation_id}/reply")
async def reply_conversation(conversation_id: str, body: SendMessageIn) -> dict[str, Any]:
    data = await store.read()
    convo = next((c for c in data["conversations"] if c["id"] == conversation_id), None)
    if not convo:
        raise HTTPException(404, "Conversa não encontrada")
    phone = body.phone or convo["phone"]
    channel_id = convo.get("phoneNumberId") or await active_phone_number_id(data)
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
    return [row for row in (await store.read())["flows"] if scoped(row, phoneNumberId)]


@app.post("/api/flows")
async def create_flow(body: FlowIn) -> dict[str, Any]:
    data = await store.read()
    doc = {"id": new_id("flow"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    data["flows"].append(doc)
    await store.write(data)
    return doc


@app.patch("/api/flows/{flow_id}")
async def update_flow(flow_id: str, body: FlowIn) -> dict[str, Any]:
    data = await store.read()
    flow = next((f for f in data["flows"] if f["id"] == flow_id), None)
    if not flow:
        raise HTTPException(404, "Fluxo não encontrado")
    flow.update(body.model_dump())
    flow["updatedAt"] = now_iso()
    await store.write(data)
    return flow


@app.delete("/api/flows/{flow_id}")
async def delete_flow(flow_id: str) -> dict[str, Any]:
    data = await store.read()
    before = len(data["flows"])
    data["flows"] = [f for f in data["flows"] if f["id"] != flow_id]
    await store.write(data)
    return {"deleted": before - len(data["flows"])}


@app.get("/api/automations")
async def list_automations(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    return [row for row in (await store.read())["automations"] if scoped(row, phoneNumberId)]


@app.post("/api/automations")
async def create_automation(body: AutomationIn) -> dict[str, Any]:
    data = await store.read()
    doc = {"id": new_id("auto"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    data["automations"].append(doc)
    await store.write(data)
    return doc


@app.patch("/api/automations/{automation_id}")
async def update_automation(automation_id: str, body: AutomationIn) -> dict[str, Any]:
    data = await store.read()
    automation = next((a for a in data["automations"] if a["id"] == automation_id), None)
    if not automation:
        raise HTTPException(404, "Automação não encontrada")
    automation.update(body.model_dump())
    automation["updatedAt"] = now_iso()
    await store.write(data)
    return automation


@app.delete("/api/automations/{automation_id}")
async def delete_automation(automation_id: str) -> dict[str, Any]:
    data = await store.read()
    before = len(data["automations"])
    data["automations"] = [a for a in data["automations"] if a["id"] != automation_id]
    await store.write(data)
    return {"deleted": before - len(data["automations"])}


@app.get("/api/campaigns")
async def list_campaigns(phoneNumberId: Optional[str] = None) -> list[dict[str, Any]]:
    rows = [row for row in (await store.read())["campaigns"] if scoped(row, phoneNumberId)]
    return sorted(rows, key=lambda c: c.get("createdAt", ""), reverse=True)


@app.post("/api/campaigns/estimate")
async def estimate_campaign(body: TemplateCampaignIn) -> dict[str, Any]:
    data = await store.read()
    included = contacts_for_campaign(data, TemplateCampaignIn(**{**body.model_dump(), "exclusionListIds": []}))
    final = contacts_for_campaign(data, body)
    excluded_ids = {c["id"] for c in included} - {c["id"] for c in final}
    return {
        "included": len(included),
        "excluded": len(excluded_ids),
        "receivers": len(final),
    }


@app.post("/api/campaigns")
async def create_campaign(body: TemplateCampaignIn, background: BackgroundTasks) -> dict[str, Any]:
    data = await store.read()
    contacts = contacts_for_campaign(data, body)
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
        "results": [],
        "lastError": None,
        "config": body.model_dump(),
        "status": status,
        "createdAt": now_iso(),
    }
    data["campaigns"].append(doc)
    await store.write(data)
    if body.sendNow:
        background.add_task(execute_campaign, doc["id"])
    return doc


@app.post("/api/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, background: BackgroundTasks) -> dict[str, Any]:
    data = await store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") == "canceled":
        raise HTTPException(status_code=400, detail="Campanha cancelada")
    campaign["status"] = "running"
    campaign["lastResumeAt"] = now_iso()
    campaign["lastErrorText"] = None
    await store.write(data)
    background.add_task(execute_campaign, campaign_id, True)
    return campaign


@app.patch("/api/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignUpdateIn, background: BackgroundTasks) -> dict[str, Any]:
    data = await store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("status") in {"done", "running"} and not body.sendNow:
        raise HTTPException(status_code=400, detail="Somente campanhas agendadas, rascunhos ou falhas podem ser editadas")
    config = campaign.setdefault("config", {})
    if body.name is not None:
        campaign["name"] = body.name
        config["name"] = body.name
    if body.scheduledAt is not None:
        campaign["scheduledAt"] = body.scheduledAt or None
        config["scheduledAt"] = body.scheduledAt or None
        config["sendNow"] = False
        campaign["status"] = "scheduled" if body.scheduledAt else "draft"
    if body.batchSize is not None:
        config["batchSize"] = body.batchSize
    if body.batchPauseSeconds is not None:
        config["batchPauseSeconds"] = body.batchPauseSeconds
    campaign["updatedAt"] = now_iso()
    if body.sendNow:
        campaign["scheduledAt"] = None
        config["scheduledAt"] = None
        config["sendNow"] = True
        campaign["status"] = "running"
        campaign["lastResumeAt"] = now_iso()
        campaign["lastErrorText"] = None
        await store.write(data)
        background.add_task(execute_campaign, campaign_id, True)
        return campaign
    await store.write(data)
    return campaign


@app.post("/api/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str) -> dict[str, Any]:
    data = await store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign["status"] = "canceled"
    campaign["canceledAt"] = now_iso()
    campaign["lastErrorText"] = None
    await store.write(data)
    return campaign


@app.post("/api/campaigns/{campaign_id}/retry-failed")
async def retry_failed_campaign(campaign_id: str, background: BackgroundTasks) -> dict[str, Any]:
    data = await store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    failed_rows = [row for row in campaign.get("results") or [] if row.get("status") == "failed"]
    if not failed_rows:
        raise HTTPException(status_code=400, detail="Nenhum lead com falha para reenviar")
    failed_keys = {(row.get("contactId"), normalize_phone(str(row.get("phone") or ""))) for row in failed_rows}
    campaign["results"] = [
        row
        for row in campaign.get("results") or []
        if (row.get("contactId"), normalize_phone(str(row.get("phone") or ""))) not in failed_keys
    ]
    campaign["status"] = "running"
    campaign["lastRetryFailedAt"] = now_iso()
    campaign["lastErrorText"] = None
    summarize_campaign(campaign)
    await store.write(data)
    background.add_task(execute_campaign, campaign_id, True)
    return campaign
