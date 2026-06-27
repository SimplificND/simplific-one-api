import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


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


class NamedIn(BaseModel):
    name: str
    color: Optional[str] = None


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
    delaySeconds: int = Field(default=0, ge=0, le=86400)


class SendMessageIn(BaseModel):
    phone: str
    items: list[MessageItem]


class TemplateCampaignIn(BaseModel):
    name: str
    templateName: str
    language: str = "pt_BR"
    listIds: list[str] = []
    tagIds: list[str] = []
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
    return bool(os.getenv("META_ACCESS_TOKEN") and os.getenv("META_PHONE_NUMBER_ID"))


def upsert_contact(data: dict[str, Any], phone: str, name: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    if not normalized:
        raise HTTPException(400, "Telefone inválido")
    contact = next((c for c in data["contacts"] if c["phone"] == normalized), None)
    if contact:
        if name and not contact.get("name"):
            contact["name"] = name
        contact["updatedAt"] = now_iso()
        return contact
    contact = {
        "id": new_id("lead"),
        "name": name,
        "phone": normalized,
        "tags": [],
        "lists": [],
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    data["contacts"].append(contact)
    return contact


def conversation_for(data: dict[str, Any], phone: str, name: Optional[str] = None) -> dict[str, Any]:
    normalized = normalize_phone(phone)
    convo = next((c for c in data["conversations"] if c["phone"] == normalized), None)
    if convo:
        return convo
    convo = {
        "id": new_id("conv"),
        "phone": normalized,
        "name": name,
        "unread": 0,
        "lastMessageAt": now_iso(),
        "createdAt": now_iso(),
    }
    data["conversations"].append(convo)
    return convo


def attach_labels(contact: dict[str, Any], tags: list[str], lists: list[str]) -> None:
    contact["tags"] = sorted(set(contact.get("tags") or []) | set(tags or []))
    contact["lists"] = sorted(set(contact.get("lists") or []) | set(lists or []))
    contact["updatedAt"] = now_iso()


async def meta_send(item: MessageItem, phone: str) -> dict[str, Any]:
    if not configured_meta():
        return {"mock": True, "reason": "META_ACCESS_TOKEN or META_PHONE_NUMBER_ID not configured"}

    phone_number_id = os.environ["META_PHONE_NUMBER_ID"]
    token = os.environ["META_ACCESS_TOKEN"]
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/messages"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(phone),
    }
    if item.type == "template":
        payload.update({
            "type": "template",
            "template": {"name": item.templateName, "language": {"code": item.language}},
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
    contact = upsert_contact(data, phone)
    convo = conversation_for(data, phone, contact.get("name"))
    results: list[dict[str, Any]] = []
    for index, item in enumerate(items):
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
            "direction": "out",
            "type": item.type,
            "text": item.text or item.caption or item.templateName or item.mediaUrl,
            "payload": item.model_dump(),
            "status": status,
            "source": source,
            "providerResponse": response,
            "error": error,
            "createdAt": now_iso(),
        }
        data["messages"].append(msg)
        results.append(msg)
    convo["lastMessageAt"] = now_iso()
    await store.write(data)
    return results


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
    contact = upsert_contact(data, phone, inbound.get("name"))
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
                    "name": contacts.get(phone),
                    "type": msg_type,
                    "text": text,
                    "buttonText": button_text,
                    "payload": raw,
                    "createdAt": now_iso(),
                })
    return messages


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": APP_NAME,
        "time": now_iso(),
        "metaConfigured": configured_meta(),
        "phoneNumberId": bool(os.getenv("META_PHONE_NUMBER_ID")),
        "wabaId": bool(os.getenv("META_WABA_ID")),
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
    for inbound in inbound_messages:
        contact = upsert_contact(data, inbound["phone"], inbound.get("name"))
        convo = conversation_for(data, contact["phone"], contact.get("name"))
        convo["unread"] = int(convo.get("unread") or 0) + 1
        convo["lastMessageAt"] = inbound["createdAt"]
        data["messages"].append({
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": contact["id"],
            "phone": contact["phone"],
            "direction": "in",
            "type": inbound.get("type"),
            "text": inbound.get("text"),
            "payload": inbound.get("payload"),
            "status": "received",
            "createdAt": inbound["createdAt"],
        })
    await store.write(data)
    for inbound in inbound_messages:
        background.add_task(run_matching_automations, inbound["phone"], inbound)
    return {"received": True, "time": now_iso(), "messages": len(inbound_messages)}


@app.get("/api/settings")
async def settings() -> dict[str, Any]:
    return {"metaGraphVersion": META_GRAPH_VERSION, **store.read().get("settings", {})}


@app.get("/api/templates")
async def list_templates() -> list[dict[str, Any]]:
    return store.read()["templates"]


@app.post("/api/templates")
async def create_template(body: TemplateIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("tpl"), **body.model_dump(), "createdAt": now_iso()}
    data["templates"].append(doc)
    await store.write(data)
    return doc


@app.get("/api/contacts")
async def list_contacts() -> list[dict[str, Any]]:
    return sorted(store.read()["contacts"], key=lambda c: c.get("createdAt", ""), reverse=True)


@app.post("/api/contacts")
async def create_contact(body: LeadIn) -> dict[str, Any]:
    data = store.read()
    contact = upsert_contact(data, body.phone, body.name)
    attach_labels(contact, body.tags, body.lists)
    await store.write(data)
    return contact


@app.post("/api/contacts/import")
async def import_contacts(rows: list[LeadIn]) -> dict[str, Any]:
    data = store.read()
    count = 0
    for row in rows:
        contact = upsert_contact(data, row.phone, row.name)
        attach_labels(contact, row.tags, row.lists)
        count += 1
    await store.write(data)
    return {"count": count}


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
        "messages": [m for m in data["messages"] if m["conversationId"] == conversation_id],
    }


@app.post("/api/messages/send")
async def send_message(body: SendMessageIn) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(400, "Adicione ao menos uma mensagem")
    results = await send_sequence(body.phone, body.items, source="manual")
    return {"sent": len([r for r in results if r["status"] == "sent"]), "results": results}


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


@app.post("/api/campaigns")
async def create_campaign(body: TemplateCampaignIn) -> dict[str, Any]:
    data = store.read()
    doc = {"id": new_id("camp"), **body.model_dump(), "status": "draft", "createdAt": now_iso()}
    data["campaigns"].append(doc)
    await store.write(data)
    return doc
