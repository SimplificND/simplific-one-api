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

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = self.empty()
        merged = self.empty()
        merged.update(data)
        return merged

    async def write(self, data: dict[str, Any]) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)


store = JsonStore(STORE_PATH)


class LeadIn(BaseModel):
    name: Optional[str] = None
    phone: str
    tags: list[str] = []
    lists: list[str] = []
    customFields: dict[str, Any] = {}


class ContactUpdateIn(BaseModel):
    name: Optional[str] = None
    tags: list[str] = []
    lists: list[str] = []
    customFields: dict[str, Any] = {}


class NamedIn(BaseModel):
    name: str
    color: Optional[str] = None


class CustomFieldIn(BaseModel):
    key: str
    label: Optional[str] = None
    type: str = "text"


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
    sendNow: bool = True
    scheduledAt: Optional[str] = None


class AutomationIn(BaseModel):
    name: str
    enabled: bool = True
    triggerType: str = "contains"  # contains | exact | button | any
    triggerValue: str = ""
    addTags: list[str] = []
    addLists: list[str] = []
    items: list[MessageItem] = []


def configured_meta() -> bool:
    data = store.read()
    settings = data.get("settings", {}).get("meta", {})
    has_token = settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN")
    has_phone = (
        settings.get("phoneNumberId")
        or os.getenv("META_PHONE_NUMBER_ID")
        or next((p.get("phoneNumberId") or p.get("id") for p in data.get("phoneNumbers", []) if p.get("active")), "")
        or next(((p.get("phoneNumberId") or p.get("id")) for p in data.get("phoneNumbers", [])), "")
    )
    return bool(has_token and has_phone)


def meta_config() -> dict[str, str]:
    settings = store.read().get("settings", {}).get("meta", {})
    return {
        "accessToken": settings.get("accessToken") or os.getenv("META_ACCESS_TOKEN") or "",
        "phoneNumberId": settings.get("phoneNumberId") or os.getenv("META_PHONE_NUMBER_ID") or "",
        "wabaId": settings.get("wabaId") or os.getenv("META_WABA_ID") or "",
        "appId": settings.get("appId") or os.getenv("META_APP_ID") or "",
        "appSecret": settings.get("appSecret") or os.getenv("META_APP_SECRET") or "",
        "businessName": settings.get("businessName") or "",
    }


def active_phone_number_id(data: Optional[dict[str, Any]] = None, override: Optional[str] = None) -> str:
    if override:
        return override
    cfg = meta_config()
    if cfg["phoneNumberId"]:
        return cfg["phoneNumberId"]
    data = data or store.read()
    active = next((p for p in data.get("phoneNumbers", []) if p.get("active")), None)
    if active:
        return active.get("phoneNumberId") or active.get("id") or ""
    first = next(iter(data.get("phoneNumbers", [])), None)
    return (first or {}).get("phoneNumberId") or (first or {}).get("id") or ""


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


def template_by_name(data: dict[str, Any], name: str, language: str) -> Optional[dict[str, Any]]:
    return next((t for t in data["templates"] if t.get("name") == name and (t.get("language") == language or not language)), None)


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
        if phone_number_id:
            contact["lastPhoneNumberId"] = phone_number_id
        contact.setdefault("customFields", {})
        contact["updatedAt"] = now_iso()
        return contact
    contact = {
        "id": new_id("lead"),
        "name": name,
        "phone": normalized,
        "lastPhoneNumberId": phone_number_id,
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
    if not configured_meta():
        return {"mock": True, "reason": "META_ACCESS_TOKEN or META_PHONE_NUMBER_ID not configured"}

    cfg = meta_config()
    phone_number_id = item.phoneNumberId or active_phone_number_id()
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
    data = store.read()
    sequence_phone_number_id = next((item.phoneNumberId for item in items if item.phoneNumberId), None) or active_phone_number_id(data)
    contact = upsert_contact(data, phone, phone_number_id=sequence_phone_number_id)
    convo = conversation_for(data, phone, contact.get("name"), sequence_phone_number_id)
    results: list[dict[str, Any]] = []
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
        msg = {
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "phoneNumberId": item.phoneNumberId or sequence_phone_number_id,
            "direction": "out",
            "type": item.type,
            "text": item.text or item.caption or item.templateName or item.mediaUrl,
            "payload": item.model_dump(),
            "status": status,
            "source": source,
            "providerMessageId": meta_message_id(response),
            "providerResponse": response,
            "error": error,
            "errorText": readable_error(error),
            "createdAt": now_iso(),
        }
        data["messages"].append(msg)
        results.append(msg)
    convo["lastMessageAt"] = now_iso()
    await store.write(data)
    return results


async def set_pending_response_flow(phone: str, response_flow_id: Optional[str] = None, button_flow_map: Optional[dict[str, str]] = None, campaign_id: Optional[str] = None) -> None:
    data = store.read()
    contact = upsert_contact(data, phone)
    if button_flow_map:
        contact["pendingResponseFlows"] = button_flow_map
    elif response_flow_id:
        contact["pendingResponseFlowId"] = response_flow_id
    if campaign_id:
        contact["pendingCampaignId"] = campaign_id
    await store.write(data)


def ensure_named_list(data: dict[str, Any], name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    existing = next((x for x in data["lists"] if x.get("name", "").lower() == clean.lower()), None)
    if existing:
        return existing["id"]
    doc = {"id": new_id("list"), "name": clean, "createdAt": now_iso()}
    data["lists"].append(doc)
    return doc["id"]


def ensure_named_tag(data: dict[str, Any], name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        return ""
    existing = next((x for x in data["tags"] if x.get("name", "").lower() == clean.lower()), None)
    if existing:
        return existing["id"]
    doc = {"id": new_id("tag"), "name": clean, "color": "#84ff00", "createdAt": now_iso()}
    data["tags"].append(doc)
    return doc["id"]


async def run_flow_for_contact(phone: str, flow_id: str, source: str = "flow") -> dict[str, Any]:
    data = store.read()
    flow = next((f for f in data["flows"] if f["id"] == flow_id and f.get("enabled", True)), None)
    if not flow:
        return {"skipped": True, "reason": "flow_not_found_or_disabled"}
    contact = upsert_contact(data, phone)
    flow_phone_number_id = contact.get("lastPhoneNumberId") or active_phone_number_id(data)
    sent_items: list[MessageItem] = []
    for action in flow.get("actions") or []:
        action_type = action.get("type")
        if action_type == "delay":
            await asyncio.sleep(int(action.get("delaySeconds") or 0))
        elif action_type == "add_tags":
            attach_labels(contact, action.get("tags") or [], [])
        elif action_type == "add_lists":
            attach_labels(contact, [], action.get("lists") or [])
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
        contact_lists = set(contact.get("lists") or [])
        contact_tags = set(contact.get("tags") or [])
        in_list = not body.listIds or bool(contact_lists & set(body.listIds))
        in_tag = not body.tagIds or bool(contact_tags & set(body.tagIds))
        is_excluded = bool(contact_lists & excluded)
        if in_list and in_tag and not is_excluded:
            selected.append(contact)
    return selected


async def execute_campaign(campaign_id: str) -> None:
    data = store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if not campaign:
        return
    campaign["status"] = "running"
    campaign["startedAt"] = now_iso()
    await store.write(data)

    sent = 0
    failed = 0
    delivery_results: list[dict[str, Any]] = []
    body = TemplateCampaignIn(**campaign["config"])
    contacts = contacts_for_campaign(data, body)
    for contact in contacts:
        custom_fields = contact.get("customFields") or {}
        template_params = {
            key: custom_fields.get(field_key, contact.get(field_key, ""))
            for key, field_key in (body.parameterMap or {}).items()
        }
        result = await send_sequence(
            contact["phone"],
            [MessageItem(
                type="template",
                templateName=body.templateName,
                language=body.language,
                templateParams=template_params,
                phoneNumberId=body.phoneNumberId,
            )],
            source=f"campaign:{campaign_id}",
        )
        sent_message = next((msg for msg in result if msg["status"] == "sent"), None)
        if sent_message:
            sent += 1
        else:
            failed += 1
        row_error = next((msg.get("error") for msg in result if msg.get("error")), None)
        row_error_text = next((msg.get("errorText") for msg in result if msg.get("errorText")), None)
        if not sent_message and not row_error_text:
            row_error_text = (
                "Envio nao foi aceito, mas a Meta nao retornou motivo. "
                "Verifique se o template esta aprovado no idioma escolhido, se todos os parametros "
                "obrigatorios foram preenchidos, se o telefone tem DDI e se o Phone Number ID esta correto."
            )
        delivery_results.append({
            "contactId": contact.get("id"),
            "name": contact.get("name"),
            "phone": contact.get("phone"),
            "status": "sent" if sent_message else "failed",
            "messageIds": [msg.get("id") for msg in result],
            "providerMessageIds": [msg.get("providerMessageId") for msg in result if msg.get("providerMessageId")],
            "sentAt": sent_message.get("createdAt") if sent_message else None,
            "deliveredAt": None,
            "readAt": None,
            "clickedAt": None,
            "buttonText": None,
            "error": row_error,
            "errorText": row_error_text,
            "diagnostic": {
                "campaignId": campaign_id,
                "templateName": body.templateName,
                "language": body.language,
                "phoneNumberId": body.phoneNumberId or active_phone_number_id(),
                "phone": contact.get("phone"),
                "templateParams": template_params,
            },
            "createdAt": now_iso(),
        })
        if body.buttonFlowMap:
            await set_pending_response_flow(contact["phone"], button_flow_map=body.buttonFlowMap, campaign_id=campaign_id)
        elif body.responseFlowId:
            await set_pending_response_flow(contact["phone"], response_flow_id=body.responseFlowId, campaign_id=campaign_id)
        else:
            await set_pending_response_flow(contact["phone"], campaign_id=campaign_id)

    data = store.read()
    campaign = next((c for c in data["campaigns"] if c["id"] == campaign_id), None)
    if campaign:
        campaign.update({
            "status": "done",
            "sent": sent,
            "failed": failed,
            "delivered": sum(1 for row in delivery_results if row.get("deliveredAt")),
            "read": sum(1 for row in delivery_results if row.get("readAt")),
            "buttonClicks": sum(1 for row in delivery_results if row.get("clickedAt")),
            "results": delivery_results,
            "lastError": next((row.get("error") for row in reversed(delivery_results) if row.get("error")), None),
            "lastErrorText": next((row.get("errorText") for row in reversed(delivery_results) if row.get("errorText")), None),
            "finishedAt": now_iso(),
        })
    await store.write(data)


async def scheduled_campaign_loop() -> None:
    while True:
        try:
            data = store.read()
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
    data = store.read()
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
        data = store.read()
        contact = upsert_contact(data, phone, inbound.get("name"), channel_id)
    matches = [a for a in data["automations"] if automation_matches(a, inbound)]
    for automation in matches:
        attach_labels(contact, automation.get("addTags") or [], automation.get("addLists") or [])
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
        data = store.read()


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
    cfg = meta_config()
    return {
        "ok": True,
        "app": APP_NAME,
        "time": now_iso(),
        "metaConfigured": configured_meta(),
        "phoneNumberId": bool(cfg["phoneNumberId"]),
        "wabaId": bool(cfg["wabaId"]),
        "webhookToken": META_VERIFY_TOKEN != "change-me",
    }


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    data = store.read()
    unread = sum(1 for c in data["conversations"] if c.get("unread", 0) > 0)
    sent = sum(1 for m in data["messages"] if m.get("direction") == "out" and m.get("status") == "sent")
    failed = sum(1 for m in data["messages"] if m.get("direction") == "out" and m.get("status") == "failed")
    return {
        "contacts": len(data["contacts"]),
        "lists": len(data["lists"]),
        "tags": len(data["tags"]),
        "templates": len(data["templates"]),
        "campaigns": len(data["campaigns"]),
        "inboxUnread": unread,
        "automationRuns": len(data["automationRuns"]),
        "messagesSent": sent,
        "messagesFailed": failed,
    }


@app.get("/api/meta/webhooks/recent")
async def recent_webhooks(limit: int = 25) -> dict[str, Any]:
    data = store.read()
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
    data = store.read()
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
    data = store.read()
    meta = {**meta_config()}
    if meta.get("accessToken"):
        meta["accessTokenPreview"] = f"{meta['accessToken'][:8]}...{meta['accessToken'][-4:]}" if len(meta["accessToken"]) > 14 else "***"
        meta["accessToken"] = ""
    return {"metaGraphVersion": META_GRAPH_VERSION, "meta": meta, **data.get("settings", {})}


@app.post("/api/meta/settings")
async def save_meta_settings(body: MetaSettingsIn) -> dict[str, Any]:
    data = store.read()
    current = data.setdefault("settings", {}).setdefault("meta", {})
    update = body.model_dump()
    if not update.get("accessToken"):
        update.pop("accessToken", None)
    current.update({k: v for k, v in update.items() if v not in (None, "")})
    current["updatedAt"] = now_iso()
    await store.write(data)
    return {"saved": True, "metaConfigured": configured_meta()}


@app.post("/api/meta/sync-templates")
async def sync_meta_templates() -> dict[str, Any]:
    cfg = meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para sincronizar modelos.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/message_templates"
    params = {"fields": "name,language,status,category,components"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    payload = res.json()
    data = store.read()
    synced = []
    for tpl in payload.get("data", []) or []:
        body_preview = ""
        for component in tpl.get("components", []) or []:
            if component.get("type") == "BODY":
                body_preview = component.get("text") or ""
        doc = {
            "id": f"meta_tpl_{tpl.get('name')}_{tpl.get('language')}",
            "name": tpl.get("name"),
            "language": tpl.get("language"),
            "status": tpl.get("status"),
            "category": tpl.get("category"),
            "bodyPreview": body_preview,
            "components": tpl.get("components") or [],
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
    cfg = meta_config()
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
    cfg = meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para ativar o webhook.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/subscribed_apps"
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    response_payload: Any = res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text
    data = store.read()
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
    data = store.read()
    numbers = data["phoneNumbers"]
    cfg = meta_config()
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
    data = store.read()
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
    cfg = meta_config()
    if not cfg["accessToken"] or not cfg["wabaId"]:
        raise HTTPException(400, "Configure WABA ID e Access Token para sincronizar números.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{cfg['wabaId']}/phone_numbers"
    params = {"fields": "id,display_phone_number,verified_name,quality_rating,messaging_limit_tier,code_verification_status,name_status"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    payload = res.json()
    data = store.read()
    synced = []
    active_id = active_phone_number_id(data)
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
    cfg = meta_config()
    if not cfg["accessToken"]:
        raise HTTPException(400, "Configure Access Token para atualizar o número.")
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}"
    params = {"fields": "id,display_phone_number,verified_name,quality_rating,messaging_limit_tier,code_verification_status,name_status"}
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.get(url, params=params, headers={"Authorization": f"Bearer {cfg['accessToken']}"})
    if res.status_code >= 400:
        raise HTTPException(res.status_code, res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text)
    row = res.json()
    data = store.read()
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
    cfg = meta_config()
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

    data = store.read()
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
    data = store.read()
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
    data = store.read()
    before = len(data["phoneNumbers"])
    data["phoneNumbers"] = [p for p in data["phoneNumbers"] if (p.get("phoneNumberId") or p.get("id")) != phone_number_id]
    if data.get("settings", {}).get("meta", {}).get("phoneNumberId") == phone_number_id:
        data["settings"]["meta"].pop("phoneNumberId", None)
    await store.write(data)
    return {"deleted": before - len(data["phoneNumbers"])}


@app.get("/api/templates")
async def list_templates() -> list[dict[str, Any]]:
    data = store.read()
    rows = []
    for template in data["templates"]:
        rows.append({
            **template,
            "buttons": extract_template_buttons(template),
            "params": extract_template_params(template),
        })
    return rows


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    data = store.read()
    template = next((t for t in data["templates"] if t["id"] == template_id), None)
    if not template:
        raise HTTPException(404, "Modelo não encontrado")
    return {**template, "buttons": extract_template_buttons(template), "params": extract_template_params(template)}


@app.post("/api/templates")
async def create_template(body: TemplateIn) -> dict[str, Any]:
    data = store.read()
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
    data = store.read()
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
    data = store.read()
    doc = next((m for m in data["media"] if m["id"] == media_id), None)
    if not doc or not Path(doc["path"]).exists():
        raise HTTPException(404, "Mídia não encontrada")
    return FileResponse(doc["path"], media_type=doc.get("contentType"), filename=doc.get("filename"))


@app.get("/api/contacts")
async def list_contacts() -> list[dict[str, Any]]:
    return sorted(store.read()["contacts"], key=lambda c: c.get("createdAt", ""), reverse=True)


@app.get("/api/contacts/{contact_id}")
async def get_contact(contact_id: str) -> dict[str, Any]:
    data = store.read()
    contact = next((c for c in data["contacts"] if c["id"] == contact_id), None)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    messages = [m for m in data["messages"] if m.get("contactId") == contact_id]
    return {"contact": contact, "messages": messages[-50:]}


@app.patch("/api/contacts/{contact_id}")
async def update_contact(contact_id: str, body: ContactUpdateIn) -> dict[str, Any]:
    data = store.read()
    contact = next((c for c in data["contacts"] if c["id"] == contact_id), None)
    if not contact:
        raise HTTPException(404, "Contato não encontrado")
    contact["name"] = body.name
    contact["tags"] = sorted(set(body.tags or []))
    contact["lists"] = sorted(set(body.lists or []))
    contact["customFields"] = body.customFields or {}
    contact["updatedAt"] = now_iso()
    await store.write(data)
    return contact


@app.post("/api/contacts")
async def create_contact(body: LeadIn) -> dict[str, Any]:
    data = store.read()
    contact = upsert_contact(data, body.phone, body.name)
    tag_ids = [ensure_named_tag(data, value) for value in body.tags]
    list_ids = [ensure_named_list(data, value) for value in body.lists]
    attach_labels(contact, [x for x in tag_ids if x], [x for x in list_ids if x], body.customFields)
    await store.write(data)
    return contact


@app.post("/api/contacts/import")
async def import_contacts(rows: list[LeadIn]) -> dict[str, Any]:
    data = store.read()
    count = 0
    for row in rows:
        contact = upsert_contact(data, row.phone, row.name)
        tag_ids = [ensure_named_tag(data, value) for value in row.tags]
        list_ids = [ensure_named_list(data, value) for value in row.lists]
        attach_labels(contact, [x for x in tag_ids if x], [x for x in list_ids if x], row.customFields)
        count += 1
    await store.write(data)
    return {"count": count}


@app.post("/api/contacts/import-csv")
async def import_contacts_csv(
    file: UploadFile = File(...),
    listName: Optional[str] = Form(None),
    tags: str = Form(""),
) -> dict[str, Any]:
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV sem cabeçalho")
    data = store.read()
    list_id = ensure_named_list(data, listName or Path(file.filename or "lista").stem)
    tag_ids = [ensure_named_tag(data, tag.strip()) for tag in tags.split(",") if tag.strip()]
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
        list_ids = [list_id] + [ensure_named_list(data, x) for x in row_lists]
        all_tag_ids = tag_ids + [ensure_named_tag(data, x) for x in row_tags]
        contact = upsert_contact(data, phone, name)
        attach_labels(contact, all_tag_ids, [x for x in list_ids if x], custom)
        imported += 1
    await store.write(data)
    return {"count": imported, "listId": list_id, "customFields": sorted(custom_fields)}


@app.get("/api/lists")
async def list_lists() -> list[dict[str, Any]]:
    return store.read()["lists"]


@app.post("/api/lists")
async def create_list(body: NamedIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("list"), "name": body.name, "createdAt": now_iso()}
    data["lists"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/custom-fields")
async def list_custom_fields() -> list[dict[str, Any]]:
    return store.read()["customFields"]


@app.post("/api/custom-fields")
async def create_custom_field(body: CustomFieldIn) -> dict[str, Any]:
    data = store.read()
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", body.key.strip()).strip("_")
    if not key:
        raise HTTPException(400, "Informe uma chave válida")
    doc = {"id": key, "key": key, "label": body.label or key, "type": body.type, "createdAt": now_iso()}
    existing = next((f for f in data["customFields"] if f["key"] == key), None)
    if existing:
        existing.update(doc)
    else:
        data["customFields"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/tags")
async def list_tags() -> list[dict[str, Any]]:
    return store.read()["tags"]


@app.post("/api/tags")
async def create_tag(body: NamedIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("tag"), "name": body.name, "color": body.color or "#84ff00", "createdAt": now_iso()}
    data["tags"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/inbox")
async def inbox() -> list[dict[str, Any]]:
    data = store.read()
    latest_by_convo = {}
    for msg in data["messages"]:
        latest_by_convo[msg["conversationId"]] = msg
    conversations = []
    for convo in data["conversations"]:
        conversations.append({**convo, "lastMessage": latest_by_convo.get(convo["id"])})
    return sorted(conversations, key=lambda c: c.get("lastMessageAt", ""), reverse=True)


@app.get("/api/inbox/{conversation_id}")
async def conversation(conversation_id: str) -> dict[str, Any]:
    data = store.read()
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
        "messages": [m for m in data["messages"] if m["conversationId"] == conversation_id],
    }


@app.post("/api/inbox/{conversation_id}/reply")
async def reply_conversation(conversation_id: str, body: SendMessageIn) -> dict[str, Any]:
    data = store.read()
    convo = next((c for c in data["conversations"] if c["id"] == conversation_id), None)
    if not convo:
        raise HTTPException(404, "Conversa não encontrada")
    phone = body.phone or convo["phone"]
    channel_id = convo.get("phoneNumberId") or active_phone_number_id(data)
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
async def list_flows() -> list[dict[str, Any]]:
    return store.read()["flows"]


@app.post("/api/flows")
async def create_flow(body: FlowIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("flow"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    data["flows"].append(doc)
    await store.write(data)
    return doc


@app.patch("/api/flows/{flow_id}")
async def update_flow(flow_id: str, body: FlowIn) -> dict[str, Any]:
    data = store.read()
    flow = next((f for f in data["flows"] if f["id"] == flow_id), None)
    if not flow:
        raise HTTPException(404, "Fluxo não encontrado")
    flow.update(body.model_dump())
    flow["updatedAt"] = now_iso()
    await store.write(data)
    return flow


@app.delete("/api/flows/{flow_id}")
async def delete_flow(flow_id: str) -> dict[str, Any]:
    data = store.read()
    before = len(data["flows"])
    data["flows"] = [f for f in data["flows"] if f["id"] != flow_id]
    await store.write(data)
    return {"deleted": before - len(data["flows"])}


@app.get("/api/automations")
async def list_automations() -> list[dict[str, Any]]:
    return store.read()["automations"]


@app.post("/api/automations")
async def create_automation(body: AutomationIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("auto"), **body.model_dump(), "createdAt": now_iso(), "updatedAt": now_iso()}
    data["automations"].append(doc)
    await store.write(data)
    return doc


@app.patch("/api/automations/{automation_id}")
async def update_automation(automation_id: str, body: AutomationIn) -> dict[str, Any]:
    data = store.read()
    automation = next((a for a in data["automations"] if a["id"] == automation_id), None)
    if not automation:
        raise HTTPException(404, "Automação não encontrada")
    automation.update(body.model_dump())
    automation["updatedAt"] = now_iso()
    await store.write(data)
    return automation


@app.delete("/api/automations/{automation_id}")
async def delete_automation(automation_id: str) -> dict[str, Any]:
    data = store.read()
    before = len(data["automations"])
    data["automations"] = [a for a in data["automations"] if a["id"] != automation_id]
    await store.write(data)
    return {"deleted": before - len(data["automations"])}


@app.get("/api/campaigns")
async def list_campaigns() -> list[dict[str, Any]]:
    return sorted(store.read()["campaigns"], key=lambda c: c.get("createdAt", ""), reverse=True)


@app.post("/api/campaigns/estimate")
async def estimate_campaign(body: TemplateCampaignIn) -> dict[str, Any]:
    data = store.read()
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
    data = store.read()
    contacts = contacts_for_campaign(data, body)
    status = "running" if body.sendNow else "scheduled" if body.scheduledAt else "draft"
    doc = {
        "id": new_id("camp"),
        "name": body.name,
        "templateName": body.templateName,
        "language": body.language,
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
