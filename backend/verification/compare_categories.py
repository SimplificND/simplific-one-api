"""
Category-by-category behavioral comparison: fires the same read requests at
the OLD JSON-backed server (port 8101) and the NEW SQLite-backed server
(port 8102), both loaded with the identical production-scale synthetic
dataset, and diffs the responses.

This is read-only verification (GET endpoints, plus a couple of read-style
POSTs like /estimate that don't mutate state) -- it doesn't test writes
performed identically on both sides since that would require running the
exact same write sequence on both and is covered by the unit/integration
test suite instead. This script's job is to prove: given the SAME data,
every listing/detail endpoint returns the SAME shape and content.
"""
import json
import sys
from collections import Counter

import httpx

OLD = "http://127.0.0.1:8101"
NEW = "http://127.0.0.1:8102"

results = []


def compare(label, old_json, new_json, key_fields=None, ignore_fields=None):
    ignore_fields = set(ignore_fields or [])
    # Fields that the real app always writes pre-sorted (attach_labels /
    # mark_contact_scope both do sorted(set(...))), but the synthetic
    # dataset generator populates via rng.sample()/list literals without
    # sorting -- a data-generation artifact, not a real behavioral
    # difference. Compare as sets (order-insensitive).
    order_insensitive_keys = {"tags", "lists", "phoneNumberIds"}

    def strip_nulls(obj, key=None):
        """Drop any dict key whose value is None, and recurse. This makes
        'key present with null value' and 'key absent' compare as equal --
        which is how every real consumer (JS included) treats them, and
        which is necessary because the synthetic generator sometimes
        writes explicit nulls for fields the real app's code paths would
        simply never set on that dict at all (e.g. lastInboundAt on a
        conversation that never received an inbound message)."""
        if isinstance(obj, dict):
            return {
                k: strip_nulls(v, key=k)
                for k, v in obj.items()
                if k not in ignore_fields and v is not None
            }
        if isinstance(obj, list):
            if key in order_insensitive_keys and all(isinstance(x, str) for x in obj):
                return sorted(obj)
            return [strip_nulls(x) for x in obj]
        return obj

    old_n = strip_nulls(old_json)
    new_n = strip_nulls(new_json)

    if isinstance(old_n, list) and isinstance(new_n, list) and key_fields:
        def keyfn(row):
            return tuple(row.get(f) for f in key_fields)
        old_by_key = {keyfn(r): r for r in old_n}
        new_by_key = {keyfn(r): r for r in new_n}
        missing_in_new = set(old_by_key) - set(new_by_key)
        extra_in_new = set(new_by_key) - set(old_by_key)
        mismatched = []
        for k in set(old_by_key) & set(new_by_key):
            if old_by_key[k] != new_by_key[k]:
                mismatched.append(k)
        ok = not missing_in_new and not extra_in_new and not mismatched
        detail = f"count old={len(old_n)} new={len(new_n)}, missing={len(missing_in_new)}, extra={len(extra_in_new)}, mismatched={len(mismatched)}"
        if mismatched:
            k = mismatched[0]
            o, n = old_by_key[k], new_by_key[k]
            diff_keys = {kk for kk in set(o.keys()) | set(n.keys()) if o.get(kk) != n.get(kk)}
            detail += f"\n  sample mismatch key={k}, differing subkeys={diff_keys}"
            for dk in diff_keys:
                detail += f"\n    old.{dk}={json.dumps(o.get(dk), sort_keys=True)[:300]}\n    new.{dk}={json.dumps(n.get(dk), sort_keys=True)[:300]}"
        results.append((label, ok, detail))
    else:
        ok = old_n == new_n
        detail = "exact match" if ok else f"old={json.dumps(old_n, sort_keys=True)[:1500]}\nnew={json.dumps(new_n, sort_keys=True)[:1500]}"
        results.append((label, ok, detail))


def get(base, path, params=None):
    r = httpx.get(f"{base}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def post(base, path, json_body=None):
    r = httpx.post(f"{base}{path}", json=json_body, timeout=30)
    r.raise_for_status()
    return r.json()


print("=== Category: contacts ===")
old_contacts = get(OLD, "/api/contacts")
new_contacts = get(NEW, "/api/contacts")
compare("contacts list (unscoped)", old_contacts, new_contacts, key_fields=["id"])

old_contacts_scoped = get(OLD, "/api/contacts", {"phoneNumberId": "PHONE_MAIN"})
new_contacts_scoped = get(NEW, "/api/contacts", {"phoneNumberId": "PHONE_MAIN"})
compare("contacts list (scoped PHONE_MAIN)", old_contacts_scoped, new_contacts_scoped, key_fields=["id"])

sample_contact_id = old_contacts[0]["id"]
old_contact_detail = get(OLD, f"/api/contacts/{sample_contact_id}")
new_contact_detail = get(NEW, f"/api/contacts/{sample_contact_id}")
compare("contact detail + messages", old_contact_detail, new_contact_detail)

print("=== Category: lists / tags / custom-fields / templates ===")
compare("lists (unscoped)", get(OLD, "/api/lists"), get(NEW, "/api/lists"), key_fields=["id"])
compare("tags (unscoped)", get(OLD, "/api/tags"), get(NEW, "/api/tags"), key_fields=["id"])
compare("custom-fields (unscoped)", get(OLD, "/api/custom-fields"), get(NEW, "/api/custom-fields"), key_fields=["id"])
compare("templates (unscoped)", get(OLD, "/api/templates"), get(NEW, "/api/templates"), key_fields=["id"])
compare("templates (scoped PHONE_MAIN)", get(OLD, "/api/templates", {"phoneNumberId": "PHONE_MAIN"}), get(NEW, "/api/templates", {"phoneNumberId": "PHONE_MAIN"}), key_fields=["id"])

print("=== Category: phone-numbers ===")
compare("phone-numbers", get(OLD, "/api/phone-numbers"), get(NEW, "/api/phone-numbers"), key_fields=["id"])

print("=== Category: flows / automations ===")
compare("flows (unscoped)", get(OLD, "/api/flows"), get(NEW, "/api/flows"), key_fields=["id"])
compare("automations (unscoped)", get(OLD, "/api/automations"), get(NEW, "/api/automations"), key_fields=["id"])

print("=== Category: campaigns ===")
old_campaigns = get(OLD, "/api/campaigns")
new_campaigns = get(NEW, "/api/campaigns")
compare("campaigns list (unscoped, with embedded results)", old_campaigns, new_campaigns, key_fields=["id"])

sample_campaign_id = old_campaigns[0]["id"]
old_est = post(OLD, "/api/campaigns/estimate", {
    "name": "estimate-check", "templateName": "x", "listIds": [], "tagIds": [], "exclusionListIds": [],
})
new_est = post(NEW, "/api/campaigns/estimate", {
    "name": "estimate-check", "templateName": "x", "listIds": [], "tagIds": [], "exclusionListIds": [],
})
compare("campaigns/estimate (no filters)", old_est, new_est)

print("=== Category: messages / inbox ===")
old_inbox = get(OLD, "/api/inbox")
new_inbox = get(NEW, "/api/inbox")
compare("inbox list (unscoped)", old_inbox, new_inbox, key_fields=["id"])

old_inbox_scoped = get(OLD, "/api/inbox", {"phoneNumberId": "PHONE_MAIN"})
new_inbox_scoped = get(NEW, "/api/inbox", {"phoneNumberId": "PHONE_MAIN"})
compare("inbox list (scoped PHONE_MAIN)", old_inbox_scoped, new_inbox_scoped, key_fields=["id"])

sample_convo_id = old_inbox[0]["id"]
# Note: fetching conversation detail marks it read (unread -> 0) as a
# side-effect on BOTH servers, so this is still a fair comparison (both
# start from unread as seeded, both transition the same way).
old_convo_detail = get(OLD, f"/api/inbox/{sample_convo_id}")
new_convo_detail = get(NEW, f"/api/inbox/{sample_convo_id}")
compare("inbox conversation detail", old_convo_detail, new_convo_detail)

print("=== Category: dashboard ===")
compare("dashboard (unscoped)", get(OLD, "/api/dashboard"), get(NEW, "/api/dashboard"))
compare("dashboard (scoped PHONE_MAIN)", get(OLD, "/api/dashboard", {"phoneNumberId": "PHONE_MAIN"}), get(NEW, "/api/dashboard", {"phoneNumberId": "PHONE_MAIN"}))

print("=== Category: settings ===")
compare("settings", get(OLD, "/api/settings"), get(NEW, "/api/settings"))

print("=== Category: webhooks (recent) ===")
compare("webhooks/recent", get(OLD, "/api/meta/webhooks/recent", {"limit": 50}), get(NEW, "/api/meta/webhooks/recent", {"limit": 50}))


print("\n\n================= SUMMARY =================")
ok_count = sum(1 for _, ok, _ in results if ok)
for label, ok, detail in results:
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {label}")
    if not ok:
        print(f"        {detail}")
print(f"\n{ok_count}/{len(results)} categories matched exactly")
if ok_count != len(results):
    sys.exit(1)
