-- SQLite schema for simplific-one-api.
--
-- Design notes:
--  - contacts.phone is globally unique. This matches the existing
--    upsert_contact() behavior in server.py, which looks up a contact by
--    phone alone (NOT scoped by phoneNumberId) -- a single contact can be
--    associated with multiple phoneNumberIds via contact_phone_numbers.
--  - campaign_results and messages are their own tables (not JSON blobs
--    inside campaigns/conversations) because they are the two
--    fastest-growing collections (per-contact-per-campaign rows, and every
--    inbound/outbound message respectively). Everything else stays small
--    (dozens to low thousands of rows) and is modeled as plain tables too,
--    but performance is not the concern there -- consistency and exact
--    behavioral parity with the current JSON shape is.
--  - Freeform / rarely-queried nested structures (payloads, provider
--    responses, diagnostic info, template components, flow actions/nodes)
--    are kept as JSON TEXT columns rather than further normalized, since
--    the existing code never queries into them -- it always reads/writes
--    them as opaque blobs attached to a row. Normalizing them would add
--    migration risk for zero behavioral benefit.
--  - Every table that server.py currently filters with scoped() has a
--    phone_number_id column (nullable -- NULL means "global / unscoped",
--    matching the current dict shape where the key can be absent).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contacts (
    id                          TEXT PRIMARY KEY,
    phone                       TEXT NOT NULL UNIQUE,
    name                        TEXT,
    last_phone_number_id        TEXT,
    pending_response_flow_id    TEXT,
    pending_response_flows_json TEXT,   -- button-text -> flow-id map
    pending_campaign_id         TEXT,
    custom_fields_json          TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_phone_numbers (
    contact_id      TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    phone_number_id TEXT NOT NULL,
    PRIMARY KEY (contact_id, phone_number_id)
);

CREATE TABLE IF NOT EXISTS lists (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    phone_number_id TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lists_phone_number_id ON lists(phone_number_id);

CREATE TABLE IF NOT EXISTS tags (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    color           TEXT,
    phone_number_id TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_phone_number_id ON tags(phone_number_id);

CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id     TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_tags_tag_id ON contact_tags(tag_id);

CREATE TABLE IF NOT EXISTS contact_lists (
    contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    list_id    TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, list_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_lists_list_id ON contact_lists(list_id);

CREATE TABLE IF NOT EXISTS custom_fields (
    id              TEXT PRIMARY KEY,
    key             TEXT NOT NULL,
    label           TEXT,
    type            TEXT NOT NULL DEFAULT 'text',
    phone_number_id TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'pt_BR',
    category        TEXT,
    status          TEXT,
    body_preview    TEXT,
    components_json TEXT NOT NULL DEFAULT '[]',
    phone_number_id TEXT,
    source          TEXT,
    synced_at       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_name_lang ON templates(name, language);
CREATE INDEX IF NOT EXISTS idx_templates_phone_number_id ON templates(phone_number_id);

CREATE TABLE IF NOT EXISTS flows (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    trigger_value   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    actions_json    TEXT NOT NULL DEFAULT '[]',
    nodes_json      TEXT NOT NULL DEFAULT '[]',
    edges_json      TEXT NOT NULL DEFAULT '[]',
    phone_number_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flows_phone_number_id ON flows(phone_number_id);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id                        TEXT PRIMARY KEY,   -- same as phone_number_id
    phone_number_id           TEXT NOT NULL,
    display_phone_number      TEXT,
    verified_name             TEXT,
    quality_rating            TEXT,
    messaging_limit_tier      TEXT,
    code_verification_status  TEXT,
    name_status               TEXT,
    active                    INTEGER NOT NULL DEFAULT 0,
    source                    TEXT,
    registration_status       TEXT,
    registered                INTEGER,
    registered_at             TEXT,
    last_registration_response_json TEXT,
    last_registration_error_json    TEXT,
    last_registration_error_text    TEXT,
    last_registration_error_at      TEXT,
    created_at                TEXT NOT NULL,
    synced_at                 TEXT,
    refreshed_at              TEXT
);

CREATE TABLE IF NOT EXISTS automations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    trigger_type    TEXT NOT NULL DEFAULT 'contains',
    trigger_value   TEXT NOT NULL DEFAULT '',
    add_tags_json   TEXT NOT NULL DEFAULT '[]',
    add_lists_json  TEXT NOT NULL DEFAULT '[]',
    items_json      TEXT NOT NULL DEFAULT '[]',
    phone_number_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automations_phone_number_id ON automations(phone_number_id);

CREATE TABLE IF NOT EXISTS automation_runs (
    id            TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    contact_id    TEXT,
    phone         TEXT,
    trigger_json  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_automation_id ON automation_runs(automation_id);

CREATE TABLE IF NOT EXISTS conversations (
    id                     TEXT PRIMARY KEY,
    phone                  TEXT NOT NULL,
    phone_number_id        TEXT,
    name                   TEXT,
    unread                 INTEGER NOT NULL DEFAULT 0,
    last_message_at        TEXT,
    last_inbound_at        TEXT,
    display_phone_number   TEXT,
    created_at             TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_phone_scope ON conversations(phone, COALESCE(phone_number_id, ''));
CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at ON conversations(last_message_at);

CREATE TABLE IF NOT EXISTS messages (
    id                      TEXT PRIMARY KEY,
    conversation_id         TEXT NOT NULL,
    contact_id              TEXT,
    phone                   TEXT,
    phone_number_id         TEXT,
    display_phone_number    TEXT,
    direction               TEXT NOT NULL,   -- 'in' | 'out'
    type                    TEXT,
    text                    TEXT,
    payload_json            TEXT,
    status                  TEXT,
    source                  TEXT,
    provider_message_id     TEXT,
    provider_response_json  TEXT,
    error_json              TEXT,
    error_text              TEXT,
    delivery_status         TEXT,
    delivery_payload_json   TEXT,
    delivery_updated_at     TEXT,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_contact_id ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_messages_provider_message_id ON messages(provider_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);

CREATE TABLE IF NOT EXISTS campaigns (
    id                    TEXT PRIMARY KEY,
    name                  TEXT,
    template_name         TEXT,
    language              TEXT,
    phone_number_id       TEXT,
    response_flow_id      TEXT,
    scheduled_at          TEXT,
    target_count          INTEGER NOT NULL DEFAULT 0,
    sent                  INTEGER NOT NULL DEFAULT 0,
    failed                INTEGER NOT NULL DEFAULT 0,
    delivered             INTEGER NOT NULL DEFAULT 0,
    read                  INTEGER NOT NULL DEFAULT 0,
    button_clicks         INTEGER NOT NULL DEFAULT 0,
    last_error_json       TEXT,
    last_error_text       TEXT,
    config_json           TEXT NOT NULL,
    status                TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    last_progress_at      TEXT,
    finished_at           TEXT,
    failed_at             TEXT,
    canceled_at           TEXT,
    last_resume_at        TEXT,
    last_retry_failed_at  TEXT,
    last_status_at        TEXT,
    last_click_at         TEXT,
    updated_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaigns_phone_number_id ON campaigns(phone_number_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS campaign_results (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id              TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id               TEXT,
    name                     TEXT,
    phone                    TEXT,
    status                   TEXT,
    message_ids_json         TEXT NOT NULL DEFAULT '[]',
    sent_at                  TEXT,
    delivered_at             TEXT,
    read_at                  TEXT,
    clicked_at               TEXT,
    button_text              TEXT,
    error_json               TEXT,
    error_text               TEXT,
    diagnostic_json          TEXT,
    created_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaign_results_campaign_id ON campaign_results(campaign_id);
CREATE INDEX IF NOT EXISTS idx_campaign_results_contact_id ON campaign_results(contact_id);
CREATE INDEX IF NOT EXISTS idx_campaign_results_phone ON campaign_results(phone);

-- A campaign_result can have provider message ids attached after the fact
-- (delivery webhooks reference providerMessageId). Normalized so
-- update_campaign_delivery_from_status can index-lookup instead of
-- scanning every result row of every campaign, which is what the current
-- JSON-blob implementation does today (data["campaigns"] -> for each ->
-- for each result row -> check membership in a python list).
CREATE TABLE IF NOT EXISTS campaign_result_provider_ids (
    campaign_result_id INTEGER NOT NULL REFERENCES campaign_results(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL,
    PRIMARY KEY (campaign_result_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_result_provider_ids_pmid ON campaign_result_provider_ids(provider_message_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_created_at ON webhook_events(created_at);

CREATE TABLE IF NOT EXISTS media (
    id           TEXT PRIMARY KEY,
    filename     TEXT,
    content_type TEXT,
    size         INTEGER,
    path         TEXT,
    url          TEXT,
    created_at   TEXT NOT NULL
);

-- Single-blob settings store (small, rarely written -- e.g. Meta app
-- credentials). Kept as one JSON blob under a fixed key, matching current
-- usage (`data["settings"]` is always read/written wholesale).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
