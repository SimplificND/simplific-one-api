"""
Builds a synthetic one-api.json shaped like production, at production scale:
~1724 contacts, ~3700 messages, ~13 campaigns with realistic campaign_results
counts, some webhook_events, some flows/automations.

This does NOT touch production or any real data -- it's a generator for a
throwaway JSON file used to validate the migration script and the load test.

Usage:
    python generate_synthetic_dataset.py /path/to/output.json
"""
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def build(
    n_contacts: int = 1724,
    n_messages_target: int = 3700,
    n_campaigns: int = 13,
    seed: int = 42,
) -> dict:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)

    phone_numbers = [
        {
            "id": "PHONE_MAIN",
            "phoneNumberId": "PHONE_MAIN",
            "displayPhoneNumber": "+5511999990000",
            "verifiedName": "Simplific ONE",
            "qualityRating": "GREEN",
            "messagingLimitTier": "TIER_10K",
            "active": True,
            "source": "meta",
            "createdAt": iso(now - timedelta(days=200)),
        },
        {
            "id": "PHONE_SECOND",
            "phoneNumberId": "PHONE_SECOND",
            "displayPhoneNumber": "+5511999991111",
            "verifiedName": "Simplific ONE - vendas",
            "qualityRating": "YELLOW",
            "messagingLimitTier": "TIER_1K",
            "active": False,
            "source": "meta",
            "createdAt": iso(now - timedelta(days=90)),
        },
    ]
    phone_ids = [p["phoneNumberId"] for p in phone_numbers]

    tags = [
        {"id": new_id("tag"), "name": name, "color": "#84ff00", "phoneNumberId": rng.choice(phone_ids + [None]), "createdAt": iso(now - timedelta(days=rng.randint(1, 200)))}
        for name in ["VIP", "Frio", "Quente", "Inadimplente", "Recorrente", "Novo Lead", "Blacklist"]
    ]
    lists = [
        {"id": new_id("list"), "name": name, "phoneNumberId": rng.choice(phone_ids + [None]), "createdAt": iso(now - timedelta(days=rng.randint(1, 200)))}
        for name in ["Leads Importados", "Clientes Ativos", "Trial Expirado", "Evento Julho", "Blacklist"]
    ]
    custom_fields = [
        {"id": f"global_{key}", "key": key, "label": label, "type": "text", "phoneNumberId": None, "createdAt": iso(now - timedelta(days=150))}
        for key, label in [("cidade", "Cidade"), ("plano", "Plano"), ("origem", "Origem")]
    ]
    templates = [
        {
            "id": new_id("tpl"),
            "name": name,
            "language": "pt_BR",
            "category": "MARKETING",
            "status": "APPROVED",
            "bodyPreview": f"Ola {{{{1}}}}, {name}!",
            "components": [{"type": "BODY", "text": f"Ola {{{{1}}}}, {name}!"}],
            "phoneNumberId": rng.choice(phone_ids),
            "source": "meta",
            "syncedAt": iso(now - timedelta(days=5)),
            "createdAt": iso(now - timedelta(days=100)),
        }
        for name in ["promo_julho", "lembrete_pagamento", "boas_vindas", "reativacao"]
    ]
    flows = [
        {
            "id": new_id("flow"),
            "name": "Fluxo de boas-vindas",
            "triggerValue": None,
            "enabled": True,
            "actions": [
                {"type": "add_tags", "text": None, "mediaUrl": None, "caption": None, "tags": [tags[0]["id"]], "lists": [], "delaySeconds": 0},
                {"type": "send_message", "text": "Bem-vindo!", "mediaUrl": None, "caption": None, "tags": [], "lists": [], "delaySeconds": 0},
            ],
            "nodes": [],
            "edges": [],
            "phoneNumberId": "PHONE_MAIN",
            "createdAt": iso(now - timedelta(days=80)),
            "updatedAt": iso(now - timedelta(days=10)),
        },
        {
            "id": new_id("flow"),
            "name": "Fluxo pos-clique",
            "triggerValue": None,
            "enabled": True,
            "actions": [
                {"type": "delay", "text": None, "mediaUrl": None, "caption": None, "tags": [], "lists": [], "delaySeconds": 30},
                {"type": "send_message", "text": "Aqui esta o material.", "mediaUrl": None, "caption": None, "tags": [], "lists": [], "delaySeconds": 0},
            ],
            "nodes": [],
            "edges": [],
            "phoneNumberId": None,
            "createdAt": iso(now - timedelta(days=60)),
            "updatedAt": iso(now - timedelta(days=60)),
        },
    ]
    automations = [
        {
            "id": new_id("auto"),
            "name": "Responde OI",
            "enabled": True,
            "triggerType": "contains",
            "triggerValue": "oi",
            "addTags": [tags[2]["id"]],
            "addLists": [],
            "items": [{"type": "text", "text": "Ola! Como posso ajudar?", "language": "pt_BR", "templateParams": {}, "delaySeconds": 0}],
            "phoneNumberId": "PHONE_MAIN",
            "createdAt": iso(now - timedelta(days=70)),
            "updatedAt": iso(now - timedelta(days=70)),
        },
        {
            "id": new_id("auto"),
            "name": "Botao bloquear contato",
            "enabled": True,
            "triggerType": "button",
            "triggerValue": "Bloquear contato",
            "addTags": [],
            "addLists": [lists[-1]["id"]],
            "items": [],
            "phoneNumberId": None,
            "createdAt": iso(now - timedelta(days=70)),
            "updatedAt": iso(now - timedelta(days=70)),
        },
    ]

    # ---- Contacts ----
    contacts = []
    for i in range(n_contacts):
        created = now - timedelta(days=rng.randint(1, 250), minutes=rng.randint(0, 1000))
        phone_number_id = rng.choice(phone_ids)
        has_name = rng.random() > 0.05  # ~5% missing name (edge case)
        has_custom_fields = rng.random() > 0.3
        contact_tags = rng.sample([t["id"] for t in tags], k=rng.randint(0, 3))
        contact_lists = rng.sample([l["id"] for l in lists], k=rng.randint(0, 2))
        contact = {
            "id": new_id("lead"),
            "name": (f"Contato {i}" if has_name else None),
            "phone": f"5511{900000000 + i}",
            "lastPhoneNumberId": phone_number_id,
            "phoneNumberIds": [phone_number_id] if rng.random() > 0.1 else [phone_number_id, rng.choice(phone_ids)],
            "tags": contact_tags,
            "lists": contact_lists,
            "customFields": (
                {"cidade": rng.choice(["SP", "RJ", "BH", "POA"]), "plano": rng.choice(["basico", "pro"])}
                if has_custom_fields else {}
            ),
            "createdAt": iso(created),
            "updatedAt": iso(created + timedelta(days=rng.randint(0, 5))),
        }
        if rng.random() < 0.1:
            contact["pendingCampaignId"] = None
        contacts.append(contact)
    # dedup safety (phones must be unique, by construction they already are)

    # ---- Conversations + messages ----
    conversations = []
    messages = []
    convo_by_contact = {}
    for contact in contacts:
        if rng.random() > 0.6:  # not every contact has a conversation
            continue
        convo = {
            "id": new_id("conv"),
            "phone": contact["phone"],
            "phoneNumberId": contact["lastPhoneNumberId"],
            "name": contact["name"],
            "unread": rng.choice([0, 0, 0, 1]),
            "lastMessageAt": iso(now - timedelta(minutes=rng.randint(0, 5000))),
            "lastInboundAt": iso(now - timedelta(minutes=rng.randint(0, 5000))) if rng.random() > 0.3 else None,
            "createdAt": contact["createdAt"],
        }
        conversations.append(convo)
        convo_by_contact[contact["id"]] = convo

    msg_count = 0
    contact_cycle = list(convo_by_contact.keys())
    while msg_count < n_messages_target and contact_cycle:
        contact_id = rng.choice(contact_cycle)
        convo = convo_by_contact[contact_id]
        direction = rng.choice(["in", "out", "out"])
        created = now - timedelta(minutes=rng.randint(0, 8000))
        msg = {
            "id": new_id("msg"),
            "conversationId": convo["id"],
            "contactId": contact_id,
            "phone": convo["phone"],
            "phoneNumberId": convo.get("phoneNumberId"),
            "direction": direction,
            "type": rng.choice(["text", "text", "template", "image"]),
            "text": "Mensagem de teste" if direction == "in" else "Resposta automatica",
            "payload": {"text": "Mensagem de teste"} if rng.random() > 0.5 else {},
            "status": rng.choice(["sent", "sent", "failed"]) if direction == "out" else "received",
            "source": "manual",
            "providerMessageId": (new_id("wamid") if direction == "out" and rng.random() > 0.2 else None),
            "providerResponse": None,
            "error": None,
            "errorText": None,
            "createdAt": iso(created),
        }
        messages.append(msg)
        msg_count += 1

    # ---- Campaigns + campaign_results ----
    campaigns = []
    contact_pool = contacts
    for c in range(n_campaigns):
        campaign_id = new_id("camp")
        created = now - timedelta(days=rng.randint(1, 180))
        target_contacts = rng.sample(contact_pool, k=min(len(contact_pool), rng.randint(50, 400)))
        status = rng.choice(["done", "done", "done", "failed", "canceled", "running"])
        results = []
        for contact in target_contacts:
            row_status = rng.choice(["sent", "sent", "sent", "failed"])
            sent_at = iso(created + timedelta(minutes=rng.randint(1, 500)))
            delivered = rng.random() > 0.2
            read_ = delivered and rng.random() > 0.4
            clicked = read_ and rng.random() > 0.7
            provider_id = new_id("wamid") if row_status == "sent" else None
            results.append({
                "contactId": contact["id"],
                "name": contact["name"],
                "phone": contact["phone"],
                "status": row_status,
                "messageIds": [new_id("msg")],
                "providerMessageIds": [provider_id] if provider_id else [],
                "sentAt": sent_at if row_status == "sent" else None,
                "deliveredAt": iso(created + timedelta(minutes=rng.randint(1, 600))) if delivered else None,
                "readAt": iso(created + timedelta(minutes=rng.randint(1, 700))) if read_ else None,
                "clickedAt": iso(created + timedelta(minutes=rng.randint(1, 800))) if clicked else None,
                "buttonText": "Quero saber mais" if clicked else None,
                "error": None if row_status == "sent" else {"message": "Template pausado"},
                "errorText": None if row_status == "sent" else "Template pausado",
                "diagnostic": {
                    "campaignId": campaign_id,
                    "templateName": rng.choice(templates)["name"],
                    "language": "pt_BR",
                    "phoneNumberId": rng.choice(phone_ids),
                    "phone": contact["phone"],
                    "templateParams": {"1": contact["name"] or ""},
                },
                "createdAt": sent_at,
            })
        campaign = {
            "id": campaign_id,
            "name": f"Campanha {c+1}",
            "templateName": rng.choice(templates)["name"],
            "language": "pt_BR",
            "phoneNumberId": rng.choice(phone_ids),
            "responseFlowId": rng.choice(flows)["id"] if rng.random() > 0.5 else None,
            "scheduledAt": None,
            "targetCount": len(target_contacts),
            "sent": sum(1 for r in results if r["status"] == "sent"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "delivered": sum(1 for r in results if r.get("deliveredAt")),
            "read": sum(1 for r in results if r.get("readAt")),
            "buttonClicks": sum(1 for r in results if r.get("clickedAt")),
            "results": results,
            "lastError": None,
            "config": {
                "name": f"Campanha {c+1}",
                "templateName": rng.choice(templates)["name"],
                "language": "pt_BR",
                "listIds": [],
                "tagIds": [],
                "exclusionListIds": [],
                "responseFlowId": None,
                "buttonFlowMap": {},
                "parameterMap": {"1": "name"},
                "phoneNumberId": "PHONE_MAIN",
                "batchSize": 50,
                "batchPauseSeconds": 1,
                "sendNow": True,
                "scheduledAt": None,
            },
            "status": status,
            "createdAt": iso(created),
            "startedAt": iso(created),
            "lastProgressAt": iso(created + timedelta(hours=1)),
        }
        if status == "done":
            campaign["finishedAt"] = iso(created + timedelta(hours=2))
        campaigns.append(campaign)

    # edge case: one campaign with zero results (freshly created, never started)
    empty_campaign_id = new_id("camp")
    campaigns.append({
        "id": empty_campaign_id,
        "name": "Campanha vazia (rascunho)",
        "templateName": templates[0]["name"],
        "language": "pt_BR",
        "phoneNumberId": "PHONE_MAIN",
        "responseFlowId": None,
        "scheduledAt": None,
        "targetCount": 0,
        "sent": 0, "failed": 0, "delivered": 0, "read": 0, "buttonClicks": 0,
        "results": [],
        "lastError": None,
        "config": {
            "name": "Campanha vazia (rascunho)", "templateName": templates[0]["name"], "language": "pt_BR",
            "listIds": [], "tagIds": [], "exclusionListIds": [], "responseFlowId": None, "buttonFlowMap": {},
            "parameterMap": {}, "phoneNumberId": "PHONE_MAIN", "batchSize": 50, "batchPauseSeconds": 1,
            "sendNow": False, "scheduledAt": None,
        },
        "status": "draft",
        "createdAt": iso(now - timedelta(hours=2)),
    })

    # ---- webhook events ----
    webhook_events = []
    for _ in range(120):
        webhook_events.append({
            "id": new_id("evt"),
            "payload": {"entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "PHONE_MAIN"}}}]}]},
            "createdAt": iso(now - timedelta(minutes=rng.randint(0, 10000))),
        })

    automation_runs = [
        {
            "id": new_id("run"),
            "automationId": automations[0]["id"],
            "contactId": contacts[i]["id"],
            "phone": contacts[i]["phone"],
            "trigger": {"text": "oi"},
            "createdAt": iso(now - timedelta(days=rng.randint(0, 30))),
        }
        for i in rng.sample(range(len(contacts)), k=min(50, len(contacts)))
    ]

    return {
        "contacts": contacts,
        "lists": lists,
        "tags": tags,
        "templates": templates,
        "campaigns": campaigns,
        "conversations": conversations,
        "messages": messages,
        "automations": automations,
        "automationRuns": automation_runs,
        "webhookEvents": webhook_events,
        "media": [],
        "flows": flows,
        "phoneNumbers": phone_numbers,
        "customFields": custom_fields,
        "settings": {
            "meta": {
                "wabaId": "1234567890",
                "phoneNumberId": "PHONE_MAIN",
                "businessName": "Simplific",
                "webhookSubscribed": True,
                "webhookSubscribedAt": iso(now - timedelta(days=100)),
            }
        },
    }


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "synthetic_one_api.json"
    dataset = build()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    print(f"Wrote {out_path}")
    print(f"  contacts: {len(dataset['contacts'])}")
    print(f"  messages: {len(dataset['messages'])}")
    print(f"  campaigns: {len(dataset['campaigns'])}")
    print(f"  campaign_results total: {sum(len(c['results']) for c in dataset['campaigns'])}")
    print(f"  conversations: {len(dataset['conversations'])}")
    print(f"  webhookEvents: {len(dataset['webhookEvents'])}")
    print(f"  flows: {len(dataset['flows'])}, automations: {len(dataset['automations'])}, automationRuns: {len(dataset['automationRuns'])}")
