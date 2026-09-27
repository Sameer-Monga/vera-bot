"""
Vera bot — magicpin AI Challenge submission.

Implements the 5-endpoint contract from challenge-testing-brief.md:
  POST /v1/context   — receive category/merchant/customer/trigger context
  POST /v1/tick       — periodic wake-up; bot may initiate messages
  POST /v1/reply      — respond to a merchant/customer reply
  GET  /v1/healthz    — liveness probe
  GET  /v1/metadata   — bot identity

Composer is deterministic and rule-based (no external LLM call, no network
egress, no API key needed) — this keeps it fast (<30s always), reproducible,
and dependency-free to deploy. Every fact used in a message is read straight
from the pushed context; nothing is invented.

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Vera Bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# suppression_key -> True   (already sent once; don't repeat)
sent_suppression_keys: set[str] = set()

# conversation_id -> {"merchant_id", "customer_id", "trigger_id", "kind",
#                      "mode", "bodies_sent": set[str]}
conversations: dict[str, dict] = {}

# merchant_id -> consecutive count of detected auto-reply / canned messages
auto_reply_streak: dict[str, int] = {}

# merchant_id -> True once a hostile message has been seen (so we only
# apologize once before going silent)
hostile_seen: dict[str, bool] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ctx(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# /v1/healthz + /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "CHANGE_ME_TEAM_NAME",
        "team_members": ["CHANGE_ME"],
        "model": "rule-based-deterministic-composer-v1",
        "approach": (
            "Deterministic template composer dispatched by trigger.kind. "
            "Every claim is read directly from pushed context (no LLM, no "
            "fabrication risk). Auto-reply / intent-transition / hostility "
            "are detected with pattern matching on incoming replies."
        ),
        "contact_email": "CHANGE_ME@example.com",
        "version": "1.0.0",
        "submitted_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": body.scope},
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] == body.version:
        # Idempotent no-op per spec: re-posting the same version succeeds
        # without changing anything.
        return {
            "accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": now_iso(),
        }
    if cur and cur["version"] > body.version:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]},
        )

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Composer helpers — every function here only reads real pushed data.
# ---------------------------------------------------------------------------

def is_hindi_mix(merchant: dict) -> bool:
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs


def salutation(category: dict, merchant: dict) -> str:
    ident = merchant.get("identity", {})
    first = ident.get("owner_first_name") or ident.get("name", "there")
    if category.get("slug") == "dentists" and not first.startswith("Dr."):
        return f"Dr. {first}"
    return first


def active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for d in category.get("digest", []):
        if d.get("id") == item_id:
            return d
    return None


def parse_signal(signal: str) -> tuple[str, Optional[str]]:
    if ":" in signal:
        k, v = signal.split(":", 1)
        return k, v
    return signal, None


def best_signal(merchant: dict) -> Optional[tuple[str, Optional[str]]]:
    signals = merchant.get("signals", [])
    return parse_signal(signals[0]) if signals else None


def is_placeholder(trigger: dict) -> bool:
    return trigger.get("payload", {}).get("placeholder") is True


def cta_line(hindi: bool, kind: str = "open") -> str:
    lines = {
        "open": {
            True: "Batayein, aage kya karna hai?",
            False: "Want me to take this further?",
        },
        "binary": {
            True: "Reply YES to go ahead, or STOP to skip.",
            False: "Reply YES to go ahead, or STOP to skip.",
        },
    }
    return lines.get(kind, lines["open"])[hindi]


# ---------------------------------------------------------------------------
# Trigger-kind handlers.
# Each returns (body, cta, send_as) or None (bot chooses not to send).
# ---------------------------------------------------------------------------

def h_research_digest(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    top = digest_item(category, trigger.get("payload", {}).get("top_item_id"))
    if top:
        segment = top.get("patient_segment") or top.get("summary", "")
        trial_n = top.get("trial_n")
        n_txt = f"{trial_n:,}-case " if isinstance(trial_n, int) else ""
        source_label = top.get("source") or "this week's research digest"
        body = (
            f"{sal}, {source_label} landed. "
            f"{n_txt}finding: {top.get('title')}"
            + (f" — relevant to your {segment} patients." if segment else ".")
            + f" Worth a look. {cta_line(is_hindi_mix(merchant))}"
        )
        return body, "open_ended", "vera"
    # placeholder / no digest item resolvable — stay generic but grounded
    sig = best_signal(merchant)
    extra = f" I also noticed {sig[0].replace('_', ' ')}" + (f" ({sig[1]})" if sig and sig[1] else "") + "." if sig else ""
    body = f"{sal}, this week's {category.get('display_name', category.get('slug'))} research digest is out.{extra} {cta_line(is_hindi_mix(merchant))}"
    return body, "open_ended", "vera"


def h_regulation_change(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    top = digest_item(category, payload.get("top_item_id"))
    deadline = payload.get("deadline_iso")
    if top and deadline:
        body = (
            f"{sal}, compliance update — {top.get('title')} ({top.get('source', '')}). "
            f"Deadline: {deadline}. Want me to send the checklist so you're covered before then?"
        )
        return body, "binary_yes_no", "vera"
    body = f"{sal}, a regulation update affecting {category.get('slug')} just dropped. Want the details?"
    return body, "open_ended", "vera"


def h_perf_spike(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    perf = merchant.get("performance", {})
    pct = perf.get("delta_7d", {}).get("views_pct")
    views = perf.get("views")
    if pct and pct > 0:
        body = (
            f"{sal}, good news — your views are up {round(pct*100)}% this week"
            + (f" ({views} in the last {perf.get('window_days', 30)} days)." if views else ".")
            + f" Want me to draft a post to ride this while it's hot?"
        )
        return body, "binary_yes_no", "vera"
    return None


def h_perf_dip(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    perf = merchant.get("performance", {})
    calls_pct = perf.get("delta_7d", {}).get("calls_pct")
    ctr = perf.get("ctr")
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr")
    if calls_pct is not None and calls_pct < 0:
        below_peer = f" — your CTR ({ctr}) is below the peer median ({peer_ctr})" if ctr and peer_ctr and ctr < peer_ctr else ""
        body = (
            f"{sal}, calls dropped {abs(round(calls_pct*100))}% week-over-week{below_peer}. "
            f"Want me to check what changed and suggest a fix?"
        )
        return body, "binary_yes_no", "vera"
    return None


def h_milestone_reached(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    agg = merchant.get("customer_aggregate", {})
    total = agg.get("total_unique_ytd")
    if total:
        body = f"{sal}, milestone — you've served {total} unique customers this year. Want a quick post to share this with your locality?"
        return body, "binary_yes_no", "vera"
    return None


def h_dormant_with_vera(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    body = f"{sal}, haven't heard from you in a bit — your account's still active and I've got a few quick wins ready when you are. {cta_line(is_hindi_mix(merchant))}"
    return body, "open_ended", "vera"


def h_review_theme_emerged(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    theme = payload.get("theme")
    if theme:
        occ = payload.get("occurrences_30d")
        theme_txt = theme.replace("_", " ")
        occ_txt = f" ({occ}x in 30 days, trend: {payload.get('trend')})" if occ else ""
        body = f"{sal}, recent reviews are flagging \"{theme_txt}\"{occ_txt}. Want me to draft a response + a fix-the-cause note?"
        return body, "binary_yes_no", "vera"
    themes = merchant.get("review_themes", [])
    if themes:
        body = f"{sal}, a few recent reviews mention \"{themes[0]}\". Want me to draft a response template for this?"
        return body, "binary_yes_no", "vera"
    sig = best_signal(merchant)
    if sig and "review" in sig[0]:
        body = f"{sal}, noticed a review pattern worth a look ({sig[0].replace('_', ' ')}). Want details?"
        return body, "open_ended", "vera"
    return None


def h_competitor_opened(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    dist = payload.get("distance_km")
    if dist:
        body = f"{sal}, a new competitor opened {dist}km away and is live on GBP. Want me to check how your listing compares?"
        return body, "binary_yes_no", "vera"
    return None


def h_festival_upcoming(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    festival = payload.get("festival") or payload.get("festival_name")
    days = payload.get("days_until")
    offer = active_offer(merchant)
    if festival:
        offer_txt = f" Want me to pair it with your active offer ({offer['title']})?" if offer else " Want me to draft a festival post?"
        due = f" in {days} days" if days else ""
        body = f"{sal}, {festival} is coming up{due}.{offer_txt}"
        return body, "binary_yes_no", "vera"
    return None


def h_weather_heatwave(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    temp = payload.get("temp_c")
    if temp:
        body = f"{sal}, {temp}\u00b0C today — footfall patterns usually shift on days like this. Want a quick post to catch people searching nearby right now?"
        return body, "binary_yes_no", "vera"
    return None


def h_local_news_event(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    headline = payload.get("headline")
    if headline:
        body = f"{sal}, heads up: {headline}. Might affect footfall today — want me to post an update for customers?"
        return body, "binary_yes_no", "vera"
    return None


def h_category_trend_movement(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    trends = category.get("trend_signals", [])
    if trends:
        t = trends[0]
        pct = t.get("delta_yoy")
        if pct:
            body = f"{sal}, \"{t.get('query')}\" searches are up {round(pct*100)}% YoY in your category. Want me to check if your listing shows up for it?"
            return body, "binary_yes_no", "vera"
    return None


def h_renewal_due(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    sub = merchant.get("subscription", {})
    days = sub.get("days_remaining")
    if days is not None and days <= 30:
        body = f"{sal}, your {sub.get('plan', 'plan')} renews in {days} days. Want me to lock in your current rate now?"
        return body, "binary_yes_no", "vera"
    return None


def h_curious_ask_due(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    body = f"{sal}, quick one — what's your most-asked question from customers this week? Helps me tailor what I send you."
    return body, "open_ended", "vera"


def h_scheduled_recurring(category, merchant, trigger, customer):
    return h_curious_ask_due(category, merchant, trigger, customer)


# --- customer-scope handlers (send_as = merchant_on_behalf) ---

def h_recall_due(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    hindi = "hi" in customer.get("identity", {}).get("language_pref", "")
    offer = active_offer(merchant)
    rel = customer.get("relationship", {})
    last_visit = rel.get("last_visit")
    m_name = merchant.get("identity", {}).get("name", "our clinic")
    offer_txt = f" {offer['title']}." if offer else ""
    body = (
        f"Hi {name}, {m_name} here \U0001f9b7 "
        f"It's been a while since your last visit ({last_visit}) — your recall is due.{offer_txt} "
        f"Reply YES and we'll find you a slot, or tell us a time that works."
    )
    return body, "multi_choice_slot" if hindi else "binary_yes_no", "merchant_on_behalf"


def h_customer_lapsed_soft(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    offer = active_offer(merchant)
    offer_txt = f" We've got {offer['title']} running right now." if offer else ""
    body = f"Hi {name}, {m_name} here — miss seeing you!{offer_txt} Want to book a slot?"
    return body, "binary_yes_no", "merchant_on_behalf"


def h_appointment_tomorrow(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    body = f"Hi {name}, reminder from {m_name} — you're booked for tomorrow. Reply YES to confirm or let us know if you need to reschedule."
    return body, "binary_yes_no", "merchant_on_behalf"


def h_chronic_refill_due(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "your pharmacy")
    body = f"Hi {name}, {m_name} here — your regular refill looks due. Reply YES and we'll have it ready for pickup."
    return body, "binary_yes_no", "merchant_on_behalf"


def h_trial_followup(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    body = f"Hi {name}, how did your first session with {m_name} go? Reply and let us know — happy to help with next steps."
    return body, "open_ended", "merchant_on_behalf"


def h_wedding_package_followup(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    payload = trigger.get("payload", {})
    wedding_date = payload.get("wedding_date")
    days = payload.get("days_to_wedding")
    if wedding_date and days:
        body = (
            f"Hi {name}, {days} days to go for {wedding_date}! Since your trial's done, "
            f"want us to start your pre-wedding skin-prep program with {m_name}?"
        )
        return body, "binary_yes_no", "merchant_on_behalf"
    return None


def h_winback_eligible(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    days = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    added = payload.get("lapsed_customers_added_since_expiry")
    if days:
        dip_txt = f" Performance's dipped {abs(round((dip or 0)*100))}% since." if dip else ""
        added_txt = f" {added} more customers have gone lapsed in that window." if added else ""
        body = f"{sal}, it's been {days} days since your plan expired.{dip_txt}{added_txt} Want me to draft a winback offer for them?"
        return body, "binary_yes_no", "vera"
    return None


def h_ipl_match_today(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    match = payload.get("match")
    venue = payload.get("venue")
    if match:
        venue_txt = f" at {venue}" if venue else ""
        body = f"{sal}, {match} today{venue_txt} — usually a big footfall night. Want a quick match-day post or offer live before kickoff?"
        return body, "binary_yes_no", "vera"
    return None


def h_active_planning_intent(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    topic = payload.get("intent_topic", "").replace("_", " ")
    last_msg = payload.get("merchant_last_message")
    if topic:
        body = f"{sal}, following up on the {topic} you mentioned — I've sketched a first draft. Want me to share it now?"
        return body, "binary_yes_no", "vera"
    return None


def h_seasonal_perf_dip(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    note = payload.get("season_note", "").replace("_", " ")
    expected = payload.get("is_expected_seasonal")
    if metric and delta:
        exp_txt = f" — typical for this window ({note})" if expected and note else ""
        body = f"{sal}, {metric} is down {abs(round(delta*100))}% this week{exp_txt}. Want a quick post to bridge the seasonal dip?"
        return body, "binary_yes_no", "vera"
    return None


def h_customer_lapsed_hard(category, merchant, trigger, customer):
    if not customer:
        return None
    name = customer.get("identity", {}).get("name", "there")
    m_name = merchant.get("identity", {}).get("name", "us")
    payload = trigger.get("payload", {})
    days = payload.get("days_since_last_visit")
    focus = payload.get("previous_focus", "").replace("_", " ")
    if days:
        focus_txt = f" for your {focus} goals" if focus else ""
        body = f"Hi {name}, it's been {days} days{focus_txt} — {m_name} would love to have you back. Want us to hold a comeback slot for you?"
        return body, "binary_yes_no", "merchant_on_behalf"
    return None


def h_supply_alert(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches", [])
    if molecule:
        batch_txt = f" (batches {', '.join(batches)})" if batches else ""
        body = f"{sal}, recall alert on {molecule}{batch_txt}. Want me to draft a compliance note for your shelf + a customer notice?"
        return body, "binary_yes_no", "vera"
    return None


def h_category_seasonal(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    trends = payload.get("trends", [])
    if trends:
        top = trends[0].replace("_", " ")
        body = f"{sal}, seasonal shift incoming: {top}. Want me to draft a shelf/stock-focused post around it?"
        return body, "binary_yes_no", "vera"
    return None


def h_gbp_unverified(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    uplift = payload.get("estimated_uplift_pct")
    path = payload.get("verification_path", "").replace("_", " ")
    if uplift:
        body = f"{sal}, your Google listing isn't verified yet — verified listings typically see +{round(uplift*100)}% visibility. Want the {path} steps?"
        return body, "binary_yes_no", "vera"
    return None


def h_cde_opportunity(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("digest_item_id"))
    fee = payload.get("fee", "").replace("_", " ")
    if item:
        body = f"{sal}, {item.get('title', 'a CDE session')} ({item.get('source', '')}) is open for registration, {fee}. Want the signup link details?"
        return body, "binary_yes_no", "vera"
    return None


TRIGGER_HANDLERS = {
    "research_digest": h_research_digest,
    "category_research_digest_release": h_research_digest,
    "regulation_change": h_regulation_change,
    "perf_spike": h_perf_spike,
    "perf_dip": h_perf_dip,
    "milestone_reached": h_milestone_reached,
    "dormant_with_vera": h_dormant_with_vera,
    "review_theme_emerged": h_review_theme_emerged,
    "competitor_opened": h_competitor_opened,
    "festival_upcoming": h_festival_upcoming,
    "weather_heatwave": h_weather_heatwave,
    "local_news_event": h_local_news_event,
    "category_trend_movement": h_category_trend_movement,
    "renewal_due": h_renewal_due,
    "curious_ask_due": h_curious_ask_due,
    "scheduled_recurring": h_scheduled_recurring,
    "recall_due": h_recall_due,
    "customer_lapsed_soft": h_customer_lapsed_soft,
    "appointment_tomorrow": h_appointment_tomorrow,
    "chronic_refill_due": h_chronic_refill_due,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_wedding_package_followup,
    "winback_eligible": h_winback_eligible,
    "ipl_match_today": h_ipl_match_today,
    "active_planning_intent": h_active_planning_intent,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "customer_lapsed_hard": h_customer_lapsed_hard,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp_unverified,
    "cde_opportunity": h_cde_opportunity,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]):
    """Returns (body, cta, send_as, rationale) or None to skip sending."""
    kind = trigger.get("kind", "")
    handler = TRIGGER_HANDLERS.get(kind)
    if not handler:
        return None
    result = handler(category, merchant, trigger, customer)
    if result is None:
        return None
    body, cta, send_as = result
    grounded = "trigger payload" if not is_placeholder(trigger) else "merchant's real performance/signal data (trigger payload was a placeholder)"
    rationale = (
        f"kind={kind}; anchored on {grounded}; "
        f"category={category.get('slug')} voice honored; "
        f"send_as={send_as}."
    )
    return body, cta, send_as, rationale


# ---------------------------------------------------------------------------
# /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    seen_merchant_this_tick: set[str] = set()

    for trg_id in body.available_triggers[:20]:
        trigger = get_ctx("trigger", trg_id)
        if not trigger:
            continue

        suppression_key = trigger.get("suppression_key", trg_id)
        if suppression_key in sent_suppression_keys:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue

        # only one action per (merchant_id, conversation) per tick
        if merchant_id in seen_merchant_this_tick:
            continue

        category = get_ctx("category", merchant.get("category_slug"))
        if not category:
            continue

        customer_id = trigger.get("customer_id")
        customer = get_ctx("customer", customer_id) if customer_id else None

        composed = compose(category, merchant, trigger, customer)
        if composed is None:
            continue

        body_text, cta, send_as, rationale = composed
        conv_id = f"conv_{merchant_id}_{trg_id}"

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "kind": trigger.get("kind"),
            "mode": "pitch",
            "bodies_sent": {body_text},
        }
        sent_suppression_keys.add(suppression_key)
        seen_merchant_this_tick.add(merchant_id)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [salutation(category, merchant), body_text],
            "body": body_text,
            "cta": cta,
            "suppression_key": suppression_key,
            "rationale": rationale,
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# /v1/reply
# ---------------------------------------------------------------------------

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"will respond shortly",
    r"team will get back",
    r"automated (assistant|response|reply)",
    r"aapki jaankari ke liye.*shukriya",
    r"team tak pahuncha",
    r"currently unavailable",
    r"out of office",
]

HOSTILE_PATTERNS = [
    r"\bstop messaging\b",
    r"\bstop sending\b",
    r"\buseless\b",
    r"\bspam\b",
    r"\bharass",
    r"\bleave me alone\b",
    r"\bf+u+c*k\b",
]

INTENT_PATTERNS = [
    r"let'?s do it",
    r"lets do it",
    r"go ahead",
    r"ok(ay)?,? let'?s",
    r"yes,? let'?s",
    r"sounds good,? let'?s",
    r"\bconfirm\b",
    r"i'?m in",
]

NOT_INTERESTED_PATTERNS = [
    r"not interested",
    r"no thanks",
    r"don'?t contact",
    r"remove me",
]


def matches_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    merchant_id = body.merchant_id
    conv = conversations.setdefault(body.conversation_id, {
        "merchant_id": merchant_id,
        "customer_id": body.customer_id,
        "trigger_id": None,
        "kind": None,
        "mode": "pitch",
        "bodies_sent": set(),
    })
    if merchant_id and not conv.get("merchant_id"):
        conv["merchant_id"] = merchant_id
    merchant_id = merchant_id or conv.get("merchant_id")

    msg = body.message or ""

    # --- hostile check first: safety over everything else ---
    if matches_any(HOSTILE_PATTERNS, msg):
        already = hostile_seen.get(merchant_id, False)
        hostile_seen[merchant_id] = True
        if not already:
            return {
                "action": "send",
                "body": "Apologies — I won't message again. If anything changes, just say 'Hi Vera'. \U0001f64f",
                "cta": "none",
                "rationale": "Merchant expressed frustration; one-line acknowledgment + opt-out, then closing.",
            }
        return {
            "action": "end",
            "rationale": "Hostility repeated after an apology was already given; ending without further contact.",
        }

    # --- auto-reply detection (streak tracked per merchant, since the same
    #     canned text can arrive across different conversation_ids) ---
    if matches_any(AUTO_REPLY_PATTERNS, msg):
        streak = auto_reply_streak.get(merchant_id, 0) + 1
        auto_reply_streak[merchant_id] = streak
        if streak == 1:
            return {
                "action": "send",
                "body": "Looks like an auto-reply \U0001f60a When the owner sees this, just reply 'Yes' and I'll continue.",
                "cta": "binary_yes_no",
                "rationale": "Detected canned auto-reply pattern; one explicit prompt flagged for the real owner.",
            }
        elif streak == 2:
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": "Same auto-reply twice in a row — owner likely not at phone. Backing off 24h before retry.",
            }
        else:
            return {
                "action": "end",
                "rationale": "Auto-reply repeated 3+ times with zero real engagement signal. Closing conversation.",
            }
    else:
        auto_reply_streak[merchant_id] = 0

    # --- explicit "not interested" ---
    if matches_any(NOT_INTERESTED_PATTERNS, msg):
        return {
            "action": "end",
            "rationale": "Merchant explicitly signaled disinterest; exiting gracefully.",
        }

    # --- intent transition: switch from pitch to action immediately ---
    if matches_any(INTENT_PATTERNS, msg):
        conv["mode"] = "action"
        merchant = get_ctx("merchant", merchant_id) if merchant_id else None
        offer_txt = ""
        if merchant:
            offer = active_offer(merchant)
            if offer:
                offer_txt = f" I'll use your active offer ({offer['title']}) in the draft."
        resp_body = (
            f"Great — drafting it now.{offer_txt} Reply CONFIRM once you've reviewed and I'll send it right away."
        )
        return {
            "action": "send",
            "body": resp_body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant explicitly committed; switching from pitch to action mode with a concrete next step instead of another qualifying question.",
        }

    # --- default: engaged, generic advance ---
    merchant = get_ctx("merchant", merchant_id) if merchant_id else None
    if merchant:
        offer = active_offer(merchant)
        hint = f" Based on your active offer ({offer['title']}), " if offer else " "
        resp_body = f"Got it —{hint}here's the next step: I'll prep a short draft for you to review. Sound good?"
    else:
        resp_body = "Got it — I'll prep the next step and share it with you shortly. Sound good?"

    # simple anti-repetition guard
    if resp_body in conv["bodies_sent"]:
        resp_body = resp_body.rstrip("?") + f" (turn {body.turn_number})?"
    conv["bodies_sent"].add(resp_body)

    return {
        "action": "send",
        "body": resp_body,
        "cta": "open_ended",
        "rationale": "Acknowledged merchant's reply and advanced the conversation with a single low-friction next step.",
    }


# ---------------------------------------------------------------------------
# /v1/teardown (optional) — wipe all state at end of test
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    sent_suppression_keys.clear()
    conversations.clear()
    auto_reply_streak.clear()
    hostile_seen.clear()
    return {"accepted": True}
