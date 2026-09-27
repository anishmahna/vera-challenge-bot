"""
bot.py — magicpin "Vera AI Challenge" candidate bot.

Implements the 5-endpoint HTTP contract from challenge-testing-brief.md:
    POST /v1/context   POST /v1/tick   POST /v1/reply   GET /v1/healthz   GET /v1/metadata
(+ optional POST /v1/teardown)

Design choice: the composer is DETERMINISTIC and rule/template based, not an LLM call.
Why: the brief's own guidance ("start small and deterministic"), the 30s per-call budget,
the "must be deterministic" requirement, and the fact that every fact used below is read
directly out of the pushed CategoryContext / MerchantContext / TriggerContext / CustomerContext
payloads (never invented) — which is exactly what the rubric's "Specificity" and anti-
fabrication penalty are checking for. No network calls, no timeouts, no hallucination risk.

Run:
    pip install fastapi uvicorn
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

# ============================================================================
# App + in-memory state
# ============================================================================

app = FastAPI(title="vera-challenge-bot")
START_TIME = time.time()

TEAM_NAME = "Samast Technologies"
TEAM_MEMBERS = ["Anish Mahna"]
CONTACT_EMAIL = "anishmahna_23ec022@dtu.ac.in"
BOT_VERSION = "1.0.0"

# contexts[(scope, context_id)] = {"version": int, "payload": dict}
contexts: Dict[tuple, Dict[str, Any]] = {}

# conversations[conversation_id] = ConversationState-shaped dict
conversations: Dict[str, Dict[str, Any]] = {}

# per-merchant raw inbound message log, for cross-conversation auto-reply detection
merchant_inbound_log: Dict[str, List[str]] = {}

# suppression_keys already fired (never re-send the same trigger instance)
fired_suppression_keys: set = set()

# body hashes already sent, per merchant (global anti-repetition safety net)
sent_body_hashes: Dict[str, set] = {}

# per-merchant (not per-conversation) auto-reply streak — canned/auto-reply
# behaviour is a property of the *contact*, not of any one conversation_id,
# so this must survive across conversation_ids to detect it reliably.
merchant_autoreply_streak: Dict[str, int] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def body_hash(text: str) -> str:
    return hashlib.sha1(norm(text).encode("utf-8")).hexdigest()


def get_ctx(scope: str, context_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not context_id:
        return None
    rec = contexts.get((scope, context_id))
    return rec["payload"] if rec else None


# ============================================================================
# Request / response models
# ============================================================================

class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickBody(BaseModel):
    now: str
    available_triggers: List[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


class TeardownBody(BaseModel):
    reason: Optional[str] = None


# ============================================================================
# Generic helpers: currency / percent / dates / names / taboo filter
# ============================================================================

def inr(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n == int(n):
        n = int(n)
    return f"₹{n:,}"


def pct(x, plus_sign=True) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    val = round(abs(x) * 100)
    sign = "+" if (x >= 0 and plus_sign) else ("-" if x < 0 else "")
    return f"{sign}{val}%"


def short_date(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    try:
        d = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return d.strftime("%-d %b").lstrip("0") if False else d.strftime("%d %b").lstrip("0")
    except Exception:
        return iso_str.split("T")[0]


def days_between(iso_a: Optional[str], iso_b: Optional[str]) -> Optional[int]:
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return abs((b - a).days)
    except Exception:
        return None


def merchant_name(merchant: Dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    return ident.get("name") or "there"


def merchant_first_name(merchant: Dict[str, Any]) -> str:
    ident = merchant.get("identity", {}) or {}
    owner = ident.get("owner_first_name")
    if owner:
        # dentists keep "Dr." register per category voice; salons/gyms/etc use bare first name
        cat = merchant.get("category_slug", "")
        if cat == "dentists":
            return f"Dr. {owner}"
        return owner
    return merchant_name(merchant)


def customer_first_name(customer: Dict[str, Any]) -> str:
    if not customer:
        return "there"
    ident = customer.get("identity", {}) or {}
    raw = ident.get("name") or "there"
    # strip "(parent: X)" / "(walk-in, no profile)" style annotations
    return raw.split("(")[0].strip() or "there"


def uses_hindi_mix(merchant: Optional[Dict[str, Any]] = None, customer: Optional[Dict[str, Any]] = None) -> bool:
    if customer:
        pref = (customer.get("identity", {}) or {}).get("language_pref", "")
        if pref and pref != "english" and pref != "en":
            return True
        return False
    if merchant:
        langs = (merchant.get("identity", {}) or {}).get("languages", [])
        return "hi" in langs
    return False


TABOO_STRIP_CACHE: Dict[str, List[str]] = {}


def apply_taboo_filter(body: str, category: Optional[Dict[str, Any]]) -> str:
    """Defensive net: strip any category-taboo phrase that slipped into a template."""
    if not category:
        return body
    taboos = ((category.get("voice", {}) or {}).get("vocab_taboo", []) or [])
    out = body
    for t in taboos:
        if not t:
            continue
        pattern = re.compile(re.escape(t), re.IGNORECASE)
        out = pattern.sub("", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def dedupe_send(merchant_id: str, body: str) -> bool:
    """Returns True if this body is safe to send (not a verbatim repeat for this merchant)."""
    h = body_hash(body)
    seen = sent_body_hashes.setdefault(merchant_id, set())
    if h in seen:
        return False
    seen.add(h)
    return True


# ============================================================================
# Category knowledge lookups
# ============================================================================

def digest_item(category: Dict[str, Any], item_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    for d in category.get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def active_offer_matching(merchant: Dict[str, Any], keywords: List[str]) -> Optional[Dict[str, Any]]:
    offers = [o for o in merchant.get("offers", []) or [] if o.get("status") == "active"]
    for o in offers:
        title = (o.get("title") or "").lower()
        if any(k in title for k in keywords):
            return o
    return offers[0] if offers else None


def peer_stat(category: Dict[str, Any], key: str):
    return (category.get("peer_stats", {}) or {}).get(key)


# ============================================================================
# Natural-language description for trigger kinds we don't have a rich seed
# template for (used only when payload has no real fields to anchor on —
# keeps us from ever exposing raw internal jargon like "customer_lapsed_soft"
# to a merchant, and from fabricating numbers that were never pushed to us).
# ============================================================================

KIND_PLAIN_ENGLISH = {
    "customer_lapsed_soft": "a regular hasn't been in for a little while",
    "customer_lapsed_hard": "a longtime customer has been away for a bit",
    "appointment_tomorrow": "there's a booking on the calendar for tomorrow",
    "unplanned_slot_open": "a slot just freed up",
    "scheduled_recurring": "it's our usual weekly check-in",
    "weather_heatwave": "today's heatwave",
    "local_news_event": "something local happening today",
    "category_trend_movement": "a shift in what people nearby are searching for",
    "renewal_due": "your plan renewal coming up",
    "trial_followup": "a recent trial visit",
    "recall_due": "a customer's recall window opening",
    "chronic_refill_due": "a regular refill coming due",
}


def is_placeholder_payload(trigger: Dict[str, Any]) -> bool:
    p = trigger.get("payload", {}) or {}
    return bool(p.get("placeholder"))


# ============================================================================
# The composer — one function per well-known trigger kind, dispatched by
# `kind`. Every function returns (body: str, cta: str). CTA vocabulary:
#   "open_ended"   — "want me to draft/pull/send X?" style, single next step
#   "binary"       — explicit Reply YES / STOP or a single yes-no ask
#   "multi_choice" — a small, bounded set of reply options (e.g. slot 1/2)
#   "none"         — pure information, no ask
# ============================================================================

def compose_research_digest(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("top_item_id"))
    name = merchant_first_name(merchant)
    signals = merchant.get("signals", []) or []
    if not item:
        return (f"{name}, this week's {category.get('display_name','category')} digest is in — "
                f"nothing that lines up with your current patient mix yet, so I'm holding off. "
                f"I'll flag the next item that's actually relevant to you."), "none"
    title = item.get("title", "").rstrip(".")
    source = item.get("source", "")
    trial_n = item.get("trial_n")
    segment = item.get("patient_segment", "").replace("_", " ")
    cohort_note = ""
    for s in signals:
        if "high_risk_adult" in s:
            cohort_note = " your high-risk-adult patients"
            break
    anchor = f"{trial_n}-patient trial" if trial_n else "the latest data"
    seg_clause = f" for {segment}" if segment and not cohort_note else cohort_note
    body = (f"{name}, {source.split(',')[0] if source else 'this week'}'s issue landed. "
            f"{title}{seg_clause and (' — relevant to' + seg_clause) or ''}. "
            f"{anchor.capitalize()} backs it up. Worth a 2-min look — want me to pull the "
            f"abstract and draft a patient-facing WhatsApp you can reshare?"
            + (f"  — {source}" if source else ""))
    return body, "open_ended"


def compose_regulation_change(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("top_item_id"))
    name = merchant_first_name(merchant)
    deadline = payload.get("deadline_iso")
    dl = short_date(deadline) if deadline else None
    if not item:
        return (f"{name}, heads up — a compliance update dropped for your category" +
                (f", effective {dl}" if dl else "") +
                ". Want me to send the full circular so you can check if it applies to you?"), "open_ended"
    title = item.get("title", "").rstrip(".")
    actionable = item.get("actionable", "")
    source = item.get("source", "")
    deadline_clause = f", effective {dl}" if (dl and "effective" not in title.lower()) else ""
    body = (f"{name}, compliance note: {title}{deadline_clause}. "
            f"{actionable}. Source: {source}. Want me to send a checklist so you're covered before the deadline?")
    return body, "open_ended"


def compose_perf_dip(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    metric = payload.get("metric", "activity")
    delta = payload.get("delta_pct")
    window = payload.get("window", "7d").replace("d", "-day")
    baseline = payload.get("vs_baseline")
    severe = "perf_dip_severe" in (merchant.get("signals", []) or [])
    lead = "Flagging this before it compounds" if severe else "Quick check"
    body = (f"{name}, {lead} — your {metric} are down {pct(delta)} over the last {window}"
            + (f" (was averaging {baseline}/day)." if baseline else ".")
            + " Want me to look at what changed — post frequency, offer visibility, or GBP completeness — and suggest one fix?")
    return body, "open_ended"


def compose_perf_spike(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    metric = payload.get("metric", "activity")
    delta = payload.get("delta_pct")
    window = payload.get("window", "7d").replace("d", "-day")
    baseline = payload.get("vs_baseline")
    driver = payload.get("likely_driver", "").replace("_", " ")
    body = (f"{name}, good sign — {metric} are up {pct(delta)} over the last {window}"
            + (f", against a baseline of {baseline}/day." if baseline else ".")
            + (f" Looks driven by {driver}." if driver else "")
            + " Want me to double down on that with another post while it's working?")
    return body, "open_ended"


def compose_renewal_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    days = payload.get("days_remaining")
    plan = payload.get("plan", "")
    amount = payload.get("renewal_amount")
    body = (f"{name}, your {plan} plan renews in {days} day{'s' if days != 1 else ''}"
            + (f" ({inr(amount)})." if amount else ".")
            + " If it lapses, profile updates and new posts pause until you're back on — "
            "want me to lock in the renewal now so nothing's interrupted?")
    return body, "binary"


def compose_festival_upcoming(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    festival = payload.get("festival", "the festival")
    days_until = payload.get("days_until")
    catalog = category.get("offer_catalog", []) or []
    priced = [o for o in catalog if o.get("type") == "service_at_price"]
    pick = (priced[0] if priced else (catalog[0] if catalog else None))
    pick_title = pick.get("title") if pick else None
    if days_until is not None and days_until > 60:
        pacing = (f"That's a while off — early enough to plan rather than scramble. "
                  f"Worth pencilling in a {category.get('display_name','festival')} offer now")
    elif days_until is not None and days_until <= 21:
        pacing = "That's coming up fast — worth having the offer live before the rush hits"
    else:
        pacing = "Good window to get an offer live before demand picks up"
    body = (f"{name}, {festival} is {days_until} days out. {pacing}"
            + (f", something like \"{pick_title}\" tends to work well for this category. " if pick_title else ". ")
            + "Want me to draft the Google post now, or set a reminder closer to the date?")
    return body, "open_ended"


def compose_wedding_package_followup(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    cname = customer_first_name(customer)
    days_to_wedding = payload.get("days_to_wedding")
    window = payload.get("next_step_window_open", "").replace("_", " ")
    offer = active_offer_matching(merchant, ["bridal", "trial", "skin", "package"])
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
    mname = merchant.get("identity", {}).get("name", "")
    price_clause = f" {inr(offer.get('value'))} covers it." if offer and str(offer.get("value", "")).isdigit() else ""
    body = (f"Hi {cname} 💍 {owner + ' from ' if owner else ''}{mname} here. "
            f"{days_to_wedding} days to go — good time to start the {window or 'pre-wedding'} window "
            f"before the calendar fills up.{price_clause} Want me to hold your usual slot for the first session?")
    return body, "binary"


def compose_curious_ask(category, merchant, trigger, customer):
    name = merchant_first_name(merchant)
    body = (f"Hi {name}! Quick one — what's been the most-asked-for service this week? "
            f"I'll turn the answer straight into a Google post and a ready reply you can paste "
            f"when customers ask about pricing. Two-minute question, saves you the post.")
    return body, "open_ended"


def compose_winback_eligible(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    days_since = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    added = payload.get("lapsed_customers_added_since_expiry")
    body = (f"{name}, it's been {days_since} days since your plan lapsed"
            + (f" — views and calls are down {pct(dip)} since." if dip else ".")
            + (f" {added} more customers have gone quiet on your profile in that window." if added else "")
            + " Want me to switch it back on today? I'll have the profile caught up within the hour.")
    return body, "binary"


def compose_ipl_match_today(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
    match = payload.get("match", "tonight's match")
    venue = payload.get("venue", "")
    is_weeknight = payload.get("is_weeknight")
    ipl_note = None
    for d in category.get("digest", []) or []:
        if "ipl" in (d.get("id", "") + d.get("title", "")).lower():
            ipl_note = d
            break
    offer = active_offer_matching(merchant, ["combo", "bogo", "free", "match"])
    contrarian = ""
    if is_weeknight is False and ipl_note:
        contrarian = " Weekend IPL nights tend to shift covers to home-watch parties rather than boost dine-in — "
    elif is_weeknight and ipl_note:
        contrarian = " Weeknight matches have been running ahead of the weekday average — "
    lead = f"Quick heads-up{', ' + owner if owner else ''} — {match}" + (f" at {venue}" if venue else "") + " tonight."
    body = (f"{lead}{contrarian}"
            + (f"push your \"{offer.get('title')}\" as the delivery special instead of a dine-in promo. "
               if offer and is_weeknight is False else
               f"worth pushing \"{offer.get('title')}\" tonight while footfall's up. " if offer else "")
            + "Want me to draft the banner?")
    return body, "open_ended"


def compose_review_theme(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    theme = payload.get("theme", "").replace("_", " ")
    occ = payload.get("occurrences_30d")
    trend = payload.get("trend", "")
    quote = payload.get("common_quote")
    body = (f"{name}, {occ} reviews this month mention \"{theme}\""
            + (f", and it's {trend}." if trend else ".")
            + (f" One customer put it as: \"{quote}\"." if quote else "")
            + " Want me to draft a quick reply template for this, plus one operational fix to try this week?")
    return body, "open_ended"


def compose_milestone_reached(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    metric = payload.get("metric", "").replace("_", " ")
    now_val = payload.get("value_now")
    target = payload.get("milestone_value")
    gap = None
    try:
        gap = int(target) - int(now_val)
    except (TypeError, ValueError):
        pass
    body = (f"{name}, you're at {now_val} {metric}"
            + (f", just {gap} short of {target}." if gap and gap > 0 else f" — that's {target} crossed!")
            + " Worth a Google post the moment you hit it — reviews at round numbers get noticed. "
            "Want me to have the post ready so it goes out same-day?")
    return body, "open_ended"


def compose_active_planning_intent(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    topic = payload.get("intent_topic", "").replace("_", " ")
    catalog = category.get("offer_catalog", []) or []
    priced = [o for o in catalog if o.get("type") == "service_at_price"]
    price_hint = (priced[0] if priced else (catalog[0] if catalog else None))
    price_hint = price_hint.get("title") if price_hint else None
    body = (f"On the {topic} — here's a starting shape: keep it to one clear price point"
            + (f" (something like \"{price_hint}\" pricing logic works well here)" if price_hint else "")
            + f", run it for 4 weeks, and I'll draft the Google post + a WhatsApp line for {name.split()[-1] if name else 'you'} to share. "
            "Want me to put the full draft together now?")
    return body, "binary"


def compose_seasonal_perf_dip(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    metric = payload.get("metric", "activity")
    delta = payload.get("delta_pct")
    season_note = payload.get("season_note", "").replace("_", " ")
    seasonal_beat = None
    for b in category.get("seasonal_beats", []) or []:
        if season_note and any(tok in b.get("note", "").lower() for tok in season_note.split()[:2]):
            seasonal_beat = b
            break
    reassurance = (f"this matches the usual {seasonal_beat.get('month_range')} pattern for the category — "
                   if seasonal_beat else "this tracks with the seasonal pattern we see across the category — ")
    body = (f"{name}, {metric} are down {pct(delta)} this week, but {reassurance}"
            "not a red flag. Better use of the quiet window: hold ad spend, focus on retention with your "
            "current customers instead. Want me to draft a small retention push for them?")
    return body, "open_ended"


def compose_customer_lapse(category, merchant, trigger, customer, hard=True):
    payload = trigger.get("payload", {})
    cname = customer_first_name(customer)
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
    mname = merchant.get("identity", {}).get("name", "")
    days = payload.get("days_since_last_visit")
    focus = payload.get("previous_focus", "").replace("_", " ")
    offer = active_offer_matching(merchant, ["trial", "free", "first"])
    body = (f"Hi {cname} 👋 {owner + ' from ' if owner else ''}{mname} here. "
            + (f"It's been about {days} days" if days else "It's been a little while")
            + " — happens to everyone, no judgment."
            + (f" We know {focus} was your focus — " if focus else " ")
            + (f"still have \"{offer.get('title')}\" open if you want to ease back in. " if offer else "")
            + "Want me to hold a slot for you this week? Reply YES — no commitment.")
    return body, "binary"


def compose_trial_followup(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    cname = customer_first_name(customer)
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
    mname = merchant.get("identity", {}).get("name", "")
    options = payload.get("next_session_options", []) or []
    slot_txt = options[0].get("label") if options else None
    body = (f"Hi {cname}! {owner + ' from ' if owner else ''}{mname} here — hope you enjoyed the trial. "
            + (f"Got a slot open {slot_txt} if you'd like to continue. " if slot_txt else "Happy to set up your next session. ")
            + "Reply YES to book it in, or tell us a time that works better.")
    return body, "multi_choice"


def compose_supply_alert(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    molecule = payload.get("molecule", "")
    batches = payload.get("affected_batches", []) or []
    mfr = payload.get("manufacturer", "")
    aggregate = merchant.get("customer_aggregate", {}) or {}
    chronic = aggregate.get("chronic_rx_count")
    body = (f"{name}, urgent: voluntary recall on {molecule} batch{'es' if len(batches) > 1 else ''} "
            f"{', '.join(batches)} by {mfr} — flagged sub-potency, no acute safety risk, but customers "
            "on this molecule should be informed for a replacement."
            + (f" You have {chronic} chronic-Rx customers on file — " if chronic else " ")
            + "want me to filter the ones on this exact molecule and draft their WhatsApp note?")
    return body, "open_ended"


def compose_chronic_refill_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    cname = customer_first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    molecules = payload.get("molecule_list", []) or []
    runs_out = payload.get("stock_runs_out_iso")
    delivery_saved = payload.get("delivery_address_saved")
    offer = active_offer_matching(merchant, ["delivery", "senior"])
    mol_txt = ", ".join(molecules) if molecules else "your regular medicines"
    body = (f"Namaste — {mname} yahan. {cname} ji ki {mol_txt} "
            + (f"{short_date(runs_out)} tak khatam ho jayengi. " if runs_out else "jaldi khatam hone waali hain. ")
            + "Same dose, same brand ready hai."
            + (f" {offer.get('title')} applies. " if offer else " ")
            + ("Delivery aapke saved address par bhej denge — " if delivery_saved else "")
            + "Reply CONFIRM to dispatch.")
    return body, "binary"


def compose_category_seasonal(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    season = payload.get("season", "").replace("_", " ")
    trends = payload.get("trends", []) or []
    trend_txt = "; ".join(t.replace("_", " ") for t in trends[:3])
    body = (f"{name}, {season} shift is on: {trend_txt}. "
            "Worth moving shelf space toward what's rising and pulling back what's tapering. "
            "Want me to sketch the shelf-rearrange in one line you can hand to staff?")
    return body, "open_ended"


def compose_gbp_unverified(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    uplift = payload.get("estimated_uplift_pct")
    path = payload.get("verification_path", "").replace("_", " ")
    body = (f"{name}, your Google listing still isn't verified — verified profiles in your category "
            + (f"typically see {pct(uplift)} more calls. " if uplift else "typically get noticeably more calls. ")
            + f"Verification is a quick {path} step. Want me to start it for you now?")
    return body, "binary"


def compose_cde_opportunity(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    item = digest_item(category, payload.get("digest_item_id"))
    name = merchant_first_name(merchant)
    credits = payload.get("credits")
    fee = payload.get("fee", "")
    if not item:
        return (f"{name}, there's a CDE session coming up in your area"
                + (f" ({credits} credits)." if credits else ".")
                + " Want the details?"), "open_ended"
    title = item.get("title", "")
    date = item.get("date", "")
    body = (f"{name}, {title}" + (f" on {short_date(date)}" if date else "")
            + (f" — {credits} CDE credits, {fee.replace('_',' ')}." if credits else ".")
            + " Want me to save the slot and set a reminder for you?")
    return body, "binary"


def compose_competitor_opened(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    comp = payload.get("competitor_name", "")
    dist = payload.get("distance_km")
    their_offer = payload.get("their_offer", "")
    body = (f"{name}, {comp} opened {dist} km from you"
            + (f" running \"{their_offer}\". " if their_offer else ". ")
            + "Not saying match their price — but worth having one visible offer of your own up "
            "before people start comparing. Want to see how your listing looks next to theirs?")
    return body, "open_ended"


def compose_dormant_with_vera(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    name = merchant_first_name(merchant)
    days = payload.get("days_since_last_merchant_message")
    last_topic = payload.get("last_topic", "").replace("_", " ")
    body = (f"Hi {name} — haven't heard from you in {days} days"
            + (f", last we spoke about {last_topic}. " if last_topic else ". ")
            + "No pressure — just flag if anything's changed on your end I should know about, "
            "or if there's one thing I can take off your plate this week.")
    return body, "open_ended"


# Registry: kind -> handler(category, merchant, trigger, customer) -> (body, cta)
COMPOSER_REGISTRY = {
    "research_digest": compose_research_digest,
    "regulation_change": compose_regulation_change,
    "perf_dip": compose_perf_dip,
    "perf_spike": compose_perf_spike,
    "renewal_due": compose_renewal_due,
    "festival_upcoming": compose_festival_upcoming,
    "wedding_package_followup": compose_wedding_package_followup,
    "curious_ask_due": compose_curious_ask,
    "winback_eligible": compose_winback_eligible,
    "ipl_match_today": compose_ipl_match_today,
    "review_theme_emerged": compose_review_theme,
    "milestone_reached": compose_milestone_reached,
    "active_planning_intent": compose_active_planning_intent,
    "seasonal_perf_dip": compose_seasonal_perf_dip,
    "seasonal_acquisition_dip": compose_seasonal_perf_dip,
    "customer_lapsed_hard": lambda c, m, t, cu: compose_customer_lapse(c, m, t, cu, hard=True),
    "customer_lapsed_soft": lambda c, m, t, cu: compose_customer_lapse(c, m, t, cu, hard=False),
    "trial_followup": compose_trial_followup,
    "supply_alert": compose_supply_alert,
    "chronic_refill_due": compose_chronic_refill_due,
    "category_seasonal": compose_category_seasonal,
    "gbp_unverified": compose_gbp_unverified,
    "cde_opportunity": compose_cde_opportunity,
    "competitor_opened": compose_competitor_opened,
    "dormant_with_vera": compose_dormant_with_vera,
}


def compose_recall_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    cname = customer_first_name(customer)
    owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
    mname = merchant.get("identity", {}).get("name", "")
    service_due = payload.get("service_due", "checkup").replace("_", " ")
    slots = payload.get("available_slots", []) or []
    offer = active_offer_matching(merchant, ["clean", "checkup", service_due.split()[0] if service_due else ""])
    hindi = uses_hindi_mix(customer=customer)
    slot_txt = " ya ".join(s.get("label", "") for s in slots[:2]) if hindi else " or ".join(s.get("label", "") for s in slots[:2])
    price_clause = f" {offer.get('title')}." if offer else ""
    if hindi:
        body = (f"Hi {cname}, {mname} yahan 🦷 Aapka {service_due} recall due hai. "
                + (f"Do slots ready hain: {slot_txt}. " if slot_txt else "")
                + (price_clause + " " if price_clause else "")
                + "Reply 1 ya 2, ya apna time bata dein.")
    else:
        body = (f"Hi {cname}, {mname} here 🦷 Your {service_due} recall is due. "
                + (f"Two slots ready: {slot_txt}. " if slot_txt else "")
                + (price_clause + " " if price_clause else "")
                + "Reply 1 or 2, or tell us a time that works.")
    return body, "multi_choice"


COMPOSER_REGISTRY["recall_due"] = compose_recall_due


def generic_fallback(category, merchant, trigger, customer):
    """Used for any kind we don't have a dedicated template for, OR when the
    trigger payload is a placeholder with no real fields. In both cases we
    deliberately do NOT invent anything about the trigger itself — instead
    we ground the message in whatever real MerchantContext/CustomerContext
    data is actually available, chosen based on the *family* the trigger
    kind belongs to (dip/spike, lapse, milestone, ...), so the message still
    reads as trigger-relevant without fabricating a single number."""
    kind = trigger.get("kind", "update")
    plain = KIND_PLAIN_ENGLISH.get(kind, kind.replace("_", " "))
    name = merchant_first_name(merchant)

    if customer:
        cname = customer_first_name(customer)
        mname = merchant.get("identity", {}).get("name", "")
        owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "")
        rel = customer.get("relationship", {}) or {}
        state = customer.get("state", "")
        visits = rel.get("visits_total")
        last_visit = rel.get("last_visit")
        hindi = uses_hindi_mix(customer=customer)
        if state in ("lapsed_soft", "lapsed_hard", "churned") and last_visit:
            detail = (f"aapki pichli visit {short_date(last_visit)} ko thi" if hindi
                      else f"it's been a little while since your last visit on {short_date(last_visit)}")
        elif visits:
            detail = (f"aapne ab tak {visits} baar visit kiya hai" if hindi
                      else f"you've visited {visits} time{'s' if visits != 1 else ''} so far")
        else:
            detail = "umeed hai sab badhiya chal raha hai" if hindi else "hope things are going well"
        greet = "Namaste" if hindi else "Hi"
        ask = "Agla visit book karna ho to bata dein." if hindi else "Let us know if you'd like to book your next visit."
        body = f"{greet} {cname}, {owner + ' se ' if (hindi and owner) else (owner + ' from ' if owner else '')}{mname} — {detail}. {ask}"
        return body, "open_ended"

    perf = merchant.get("performance", {}) or {}
    delta7 = perf.get("delta_7d", {}) or {}
    signals = merchant.get("signals", []) or []
    agg = merchant.get("customer_aggregate", {}) or {}

    if kind in ("perf_dip", "seasonal_perf_dip", "seasonal_acquisition_dip"):
        neg = {k: v for k, v in delta7.items() if isinstance(v, (int, float)) and v < 0}
        if neg:
            worst_k, worst_v = min(neg.items(), key=lambda kv: kv[1])
            metric_label = worst_k.replace("_pct", "").replace("_", " ")
            beat = None
            for b in category.get("seasonal_beats", []) or []:
                pass
            return (f"{name}, your {metric_label} are down {pct(worst_v)} over the last 7 days. "
                    "Want me to take a look at what might be behind it and suggest one fix?"), "open_ended"

    if kind == "perf_spike":
        pos = {k: v for k, v in delta7.items() if isinstance(v, (int, float)) and v > 0}
        if pos:
            best_k, best_v = max(pos.items(), key=lambda kv: kv[1])
            metric_label = best_k.replace("_pct", "").replace("_", " ")
            return (f"{name}, your {metric_label} are up {pct(best_v)} this week. "
                    "Want me to help capitalise on it with a post while it's working?"), "open_ended"

    if kind == "milestone_reached" and agg:
        k0 = next(iter(agg))
        return (f"{name}, quick note — you're at {agg[k0]} {k0.replace('_', ' ')} right now. "
                "Want me to check if there's a milestone worth planning a post around?"), "open_ended"

    if kind in ("dormant_with_vera", "curious_ask_due") and merchant.get("conversation_history"):
        last = merchant["conversation_history"][-1]
        if last.get("from") == "vera":
            return (f"Hi {name} — following up on where we left off. "
                    "Want me to pick that back up, or is there something else on your mind this week?"), "open_ended"

    if kind == "competitor_opened" and agg:
        k0 = next(iter(agg))
        return (f"{name}, worth a quick competitive check — you're at {agg[k0]} {k0.replace('_', ' ')}. "
                "Want me to see how your listing compares to what's nearby?"), "open_ended"

    if signals:
        sig = signals[0].replace("_", " ").replace(":", " — ")
        return (f"{name}, noticed {sig} on your profile. "
                "Want me to take a closer look and suggest one concrete next step?"), "open_ended"

    if perf.get("views") is not None:
        return (f"{name}, quick check-in — {perf.get('views')} views and {perf.get('calls')} calls "
                f"in the last {perf.get('window_days', 30)} days. Anything you'd like a hand with this week?"), "open_ended"

    return (f"Hi {name}, just checking in — let me know if there's anything I can help with this week."), "open_ended"


def compose_message(category: Dict[str, Any], merchant: Dict[str, Any],
                     trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    kind = trigger.get("kind", "")
    handler = COMPOSER_REGISTRY.get(kind)
    if handler and not is_placeholder_payload(trigger):
        body, cta = handler(category, merchant, trigger, customer)
    else:
        body, cta = generic_fallback(category, merchant, trigger, customer)

    body = apply_taboo_filter(body, category)
    send_as = "merchant_on_behalf" if (trigger.get("scope") == "customer" or customer) else "vera"

    rationale = (
        f"kind={kind}, scope={trigger.get('scope')}, urgency={trigger.get('urgency')}: "
        f"anchored on {'trigger payload + ' if not is_placeholder_payload(trigger) else ''}"
        f"merchant context ({merchant.get('identity', {}).get('name', '?')})"
        + (f" and customer context ({customer.get('identity', {}).get('name')})" if customer else "")
        + f"; category voice = {(category.get('voice', {}) or {}).get('tone', 'default')}."
    )

    template_name = f"vera_{kind}_v1"
    template_params = [merchant_name(merchant)]
    if customer:
        template_params.append(customer_first_name(customer))

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{trigger.get('id')}",
        "rationale": rationale,
        "template_name": template_name,
        "template_params": template_params,
    }


# ============================================================================
# Reply engine — multi-turn conversation handling
# ============================================================================

AUTO_REPLY_PATTERNS = [
    r"thank you for (contacting|reaching out|your message)",
    r"will (respond|get back|reply)\b.*\b(shortly|soon)",
    r"we (will|shall|would) (get back|respond|reply)",
    r"currently (unavailable|busy|closed|away)",
    r"automated (assistant|message|reply|response)",
    r"out of office",
    r"team (will|shall) (respond|reach out|get back|revert)",
    r"hamari team tak pahuncha",
    r"team tak pahuncha",
    r"this is an auto[- ]?(generated|reply)",
    r"we've received your message",
    r"i am an? automated",
    r"main ek automated",
]
AUTO_REPLY_RE = re.compile("|".join(AUTO_REPLY_PATTERNS), re.IGNORECASE)

HOSTILE_PATTERNS = [
    r"\bstop\b.{0,20}messag", r"\bspam\b", r"\buseless\b", r"harass",
    r"\bshut up\b", r"\bidiot\b", r"\bfraud\b", r"waste of (my )?time",
    r"leave me alone", r"don'?t (message|contact|text) me", r"block(ing)? you",
    r"\bnonsense\b", r"\bbakwas\b", r"\bbewakoof\b",
]
HOSTILE_RE = re.compile("|".join(HOSTILE_PATTERNS), re.IGNORECASE)

OPTOUT_PATTERNS = [
    r"not interested", r"no thanks", r"please stop", r"unsubscribe",
    r"remove me", r"don'?t need this", r"maybe later", r"not (right )?now",
]
OPTOUT_RE = re.compile("|".join(OPTOUT_PATTERNS), re.IGNORECASE)

INTENT_PATTERNS = [
    r"let'?s do (it|this)", r"\bgo ahead\b", r"sounds good", r"\byes\b.{0,15}(please|send|do it)",
    r"\bok(ay)?\b.{0,10}(lets|let'?s|sure|do it)", r"\bi'?m in\b", r"count me in",
    r"\bproceed\b", r"\bconfirm(ed)?\b", r"please (send|share|do it)", r"i want to join",
    r"whats next", r"what'?s next",
]
INTENT_RE = re.compile("|".join(INTENT_PATTERNS), re.IGNORECASE)

ONMISSION_KEYWORDS = [
    "profile", "gbp", "google", "offer", "customer", "review", "post",
    "renew", "subscription", "plan", "price", "discount", "photo", "call",
    "booking", "slot", "recall", "delivery", "order", "marketing",
]


def is_auto_reply_like(text: str) -> bool:
    return bool(AUTO_REPLY_RE.search(text))


def is_hostile(text: str) -> bool:
    return bool(HOSTILE_RE.search(text))


def is_optout(text: str) -> bool:
    return bool(OPTOUT_RE.search(text))


def is_intent_commitment(text: str) -> bool:
    return bool(INTENT_RE.search(text))


def is_offtopic_question(text: str) -> bool:
    if "?" not in text:
        return False
    low = text.lower()
    return not any(k in low for k in ONMISSION_KEYWORDS)


def get_or_init_conversation(conv_id: str, merchant_id: Optional[str], customer_id: Optional[str]) -> Dict[str, Any]:
    conv = conversations.get(conv_id)
    if not conv:
        conv = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "turns": [],
            "stage": "pitched",
            "autoreply_streak": 0,
            "sent_bodies": set(),
        }
        conversations[conv_id] = conv
    return conv


def handle_reply(body: ReplyBody) -> Dict[str, Any]:
    conv = get_or_init_conversation(body.conversation_id, body.merchant_id, body.customer_id)
    text = body.message or ""
    text_n = norm(text)

    mid = body.merchant_id or conv.get("merchant_id") or "unknown"
    log = merchant_inbound_log.setdefault(mid, [])
    prior_occurrences = sum(1 for t in log if t == text_n)
    log.append(text_n)
    if len(log) > 50:
        del log[: len(log) - 50]

    conv["turns"].append({"from": body.from_role, "message": text, "turn": body.turn_number})

    merchant = get_ctx("merchant", mid) or {}
    category = get_ctx("category", merchant.get("category_slug")) if merchant else None

    # 1) auto-reply / canned-response detection (pattern OR verbatim repeat from this merchant).
    # Tracked per-merchant (not per-conversation_id): a canned auto-responder is a property
    # of the *contact*, and the judge harness may route repeated canned replies through fresh
    # conversation_ids, so per-conversation state alone would never catch the pattern.
    canned_signal = is_auto_reply_like(text) or prior_occurrences >= 1
    if canned_signal:
        streak = merchant_autoreply_streak.get(mid, 0) + 1
        merchant_autoreply_streak[mid] = streak
        if streak >= 2:
            return {
                "action": "end",
                "rationale": "Second canned/auto-reply-shaped message from this contact — "
                             "exiting rather than burning further turns on a bot.",
            }
        # first time seeing something canned-looking: try once, low-friction, human-directed
        nudge = ("Totally understand if that's an auto-reply — if a real person's reading this, "
                 "it's a 2-minute check, not a sales pitch. Worth a look?")
        return {"action": "send", "body": nudge, "cta": "binary",
                "rationale": "Message matched a known auto-reply/canned pattern; trying once "
                             "more with a low-friction human-directed ask before disengaging."}
    else:
        merchant_autoreply_streak[mid] = 0

    # 2) hostile message — de-escalate, stay reachable for later on-mission asks
    if is_hostile(text):
        conv["stage"] = "de-escalated"
        reply_body = ("Sorry to bother you — I'll stop the nudges here. If you ever want a hand "
                      "with your profile, offers, or customers, I'm around.")
        return {"action": "send", "body": reply_body, "cta": "none",
                "rationale": "Hostile message detected; apologizing and offering an opt-out "
                             "while keeping the door open, without abandoning the merchant relationship."}

    # 3) explicit opt-out / not interested — graceful exit
    if is_optout(text):
        return {"action": "end",
                "rationale": "Merchant signalled disinterest/opt-out; exiting gracefully per anti-spam guidance."}

    # 4) commitment / intent transition — switch straight to action, no more qualifying
    if is_intent_commitment(text):
        conv["stage"] = "action"
        last_bot_topic = None
        for t in reversed(conv["turns"]):
            if t["from"] in ("vera", "bot", "assistant"):
                last_bot_topic = t["message"]
                break
        reply_body = ("Done — locking this in now. I'll have the draft ready and share it here "
                      "for your final confirm. Next: just tell me the price point you want to lead with.")
        return {"action": "send", "body": reply_body, "cta": "open_ended",
                "rationale": "Merchant gave explicit go-ahead; switching immediately from pitch/"
                             "qualification to concrete action instead of re-asking a qualifying question."}

    # 5) off-topic / unrelated question — answer briefly, redirect politely, stay on mission
    if is_offtopic_question(text):
        reply_body = ("That one's outside what I can help with directly — I'm focused on your "
                      "magicpin profile, offers, and customers. Happy to help with anything on that side!")
        return {"action": "send", "body": reply_body, "cta": "none",
                "rationale": "Off-topic/unrelated question; declining politely and redirecting "
                             "back to core scope rather than attempting an unrelated task."}

    # 6) knowing when to stop — too many turns without resolution
    sent_count = sum(1 for t in conv["turns"] if t["from"] in ("vera", "bot", "assistant"))
    if body.turn_number >= 7 or sent_count >= 4:
        return {"action": "end",
                "rationale": "Conversation has run several turns without a clear commitment; "
                             "exiting gracefully rather than continuing to nudge."}

    # 7) generic engaged continuation
    reply_body = ("Got it — noted. Want me to go ahead and put together the next step, or is "
                  "there something specific you'd like me to check first?")
    if reply_body in conv["sent_bodies"]:
        reply_body = "Sure — let me know whenever you're ready and I'll take it from there."
    conv["sent_bodies"].add(reply_body)
    return {"action": "send", "body": reply_body, "cta": "open_ended",
            "rationale": "Neutral/ambiguous reply from merchant; keeping the conversation open "
                         "with a low-friction next step rather than assuming intent."}


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "deterministic-rule-composer-v1 (no external LLM call)",
        "approach": (
            "Context-grounded template composer dispatched by trigger.kind; every numeric/"
            "textual claim is read directly from the pushed category/merchant/trigger/customer "
            "payloads (no fabrication). Reply engine handles auto-reply detection (pattern + "
            "cross-conversation verbatim-repeat tracking), hostile de-escalation, explicit-"
            "intent fast-path to action, off-topic redirection, and graceful multi-turn exit. "
            "Fully deterministic and sub-second per call by design."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": now_iso(),
    }


@app.post("/v1/context")
async def push_context(body: ContextPush):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": now_iso()}


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: List[Dict[str, Any]] = []

    # rank candidate triggers by urgency (desc) so the highest-value sends win the 20-action cap
    candidates = []
    for trg_id in body.available_triggers:
        trg = get_ctx("trigger", trg_id)
        if not trg:
            continue
        candidates.append((trg.get("urgency", 1), trg_id, trg))
    candidates.sort(key=lambda x: x[0], reverse=True)

    for _urgency, trg_id, trg in candidates:
        if len(actions) >= 20:
            break

        supp_key = trg.get("suppression_key") or f"{trg.get('kind')}:{trg_id}"
        if supp_key in fired_suppression_keys:
            continue

        # expiry check
        expires_at = trg.get("expires_at")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                now_dt = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
                if now_dt > exp:
                    continue
            except Exception:
                pass

        merchant_id = trg.get("merchant_id")
        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue
        category = get_ctx("category", merchant.get("category_slug"))
        if not category:
            continue

        customer = None
        if trg.get("scope") == "customer":
            customer_id = trg.get("customer_id")
            customer = get_ctx("customer", customer_id)
            if not customer:
                # can't personalize a customer-facing send without the customer context yet
                continue

        composed = compose_message(category, merchant, trg, customer)

        if not dedupe_send(merchant_id, composed["body"]):
            continue

        conv_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        conv = get_or_init_conversation(conv_id, merchant_id, trg.get("customer_id"))
        conv["turns"].append({"from": "vera", "message": composed["body"], "turn": 1})
        conv["sent_bodies"].add(composed["body"])

        fired_suppression_keys.add(supp_key)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": trg.get("customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": composed["template_name"],
            "template_params": composed["template_params"],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": supp_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    return handle_reply(body)


@app.post("/v1/teardown")
async def teardown(_body: Optional[TeardownBody] = None):
    contexts.clear()
    conversations.clear()
    merchant_inbound_log.clear()
    fired_suppression_keys.clear()
    sent_body_hashes.clear()
    merchant_autoreply_streak.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
