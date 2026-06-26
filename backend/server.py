import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel


APP_NAME = os.getenv("APP_NAME", "Simplific ONE API")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "change-me")

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


class LeadIn(BaseModel):
    name: str | None = None
    phone: str
    tags: list[str] = []
    lists: list[str] = []


class TemplateCampaignIn(BaseModel):
    name: str
    templateName: str
    language: str = "pt_BR"
    listIds: list[str] = []
    tagIds: list[str] = []
    scheduledAt: str | None = None


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": APP_NAME,
        "time": now_iso(),
        "metaConfigured": bool(os.getenv("META_ACCESS_TOKEN") and os.getenv("META_PHONE_NUMBER_ID")),
    }


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    return {
        "contacts": 0,
        "lists": 0,
        "tags": 0,
        "templates": 0,
        "campaigns": 0,
        "inboxUnread": 0,
        "automationRuns": 0,
    }


@app.get("/api/meta/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == META_VERIFY_TOKEN and hub_challenge:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/api/meta/webhook")
async def receive_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    # Storage and routing will be added when Meta credentials are connected.
    return {"received": True, "time": now_iso(), "entries": len(payload.get("entry", []))}


@app.get("/api/templates")
async def list_templates() -> list[dict[str, Any]]:
    return []


@app.get("/api/contacts")
async def list_contacts() -> list[dict[str, Any]]:
    return []


@app.post("/api/contacts")
async def create_contact(body: LeadIn) -> dict[str, Any]:
    return {"id": f"lead_{int(datetime.now().timestamp())}", **body.model_dump(), "createdAt": now_iso()}


@app.get("/api/inbox")
async def inbox() -> list[dict[str, Any]]:
    return []


@app.post("/api/campaigns")
async def create_campaign(body: TemplateCampaignIn) -> dict[str, Any]:
    return {
        "id": f"camp_{int(datetime.now().timestamp())}",
        **body.model_dump(),
        "status": "draft",
        "createdAt": now_iso(),
    }

