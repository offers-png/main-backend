import os, re
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import stripe
try:
    from supabase import create_client
except Exception:
    create_client = None
try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None

scanpass_routes = APIRouter()
SUPPORTED_STATES = ["NY"]
MODEL = os.getenv("SCANPASS_CLAUDE_MODEL", "claude-opus-4-7")

DISPUTE_TYPES = {
 "tag_failure":{"label":"Tag failure — my tag was active and funded but the gantry billed my plate","questions":[{"key":"tag_number","q":"What is your E-ZPass tag number?"},{"key":"tag_status_at_time","q":"Was your tag active and funded?"},{"key":"gantry_locations","q":"Which gantries / locations and dates were billed by plate?"},{"key":"amount_disputed","q":"Total dollar amount disputed."},{"key":"evidence","q":"What documentation can you attach?"}]},
 "no_notification":{"label":"No notification — I never received any mail, email, or SMS","questions":[{"key":"address_on_file","q":"What address is on file?"},{"key":"first_learned","q":"When/how did you first learn of these charges?"},{"key":"amount_disputed","q":"Total dollar amount disputed."},{"key":"time_window","q":"Date range of disputed charges?"},{"key":"evidence","q":"What documentation can you attach?"}]},
 "wrong_plate":{"label":"Wrong plate — dealer temp plate or plate not registered to me","questions":[{"key":"plate_in_question","q":"What plate number was charged?"},{"key":"ownership_status","q":"Did you ever own/operate this vehicle?"},{"key":"dealer_or_dmv","q":"Dealer temp plate details?"},{"key":"amount_disputed","q":"Total amount charged in error."},{"key":"evidence","q":"What can you attach?"}]},
 "excessive_fees":{"label":"Excessive fees — original toll was small, fees are unconstitutional","questions":[{"key":"original_toll","q":"Underlying toll amount?"},{"key":"fee_amount","q":"Total fees added?"},{"key":"time_to_compound","q":"How much time passed before fees?"},{"key":"amount_disputed","q":"Total amount demanded."},{"key":"evidence","q":"What can you attach?"}]},
 "already_collections":{"label":"Already in collections — dispute the fees, not the underlying toll","questions":[{"key":"collections_agency","q":"Which collections agency contacted you?"},{"key":"original_toll","q":"Original toll amount?"},{"key":"fee_amount","q":"Collections balance now?"},{"key":"notice_history","q":"Notice history?"},{"key":"evidence","q":"What can you attach?"}]},
 "unknown_plate":{"label":"Unknown plate — charges tied to a plate I never owned","questions":[{"key":"plate_in_question","q":"What plate number is being charged?"},{"key":"years_disputed","q":"What years/dates?"},{"key":"amount_disputed","q":"Total amount?"},{"key":"identity_proof","q":"Can you prove the plate is not yours?"},{"key":"evidence","q":"What documentation can you attach?"}]},
 "account_suspended":{"label":"Account suspended without notice","questions":[{"key":"account_number","q":"E-ZPass account number?"},{"key":"date_discovered","q":"When did you discover suspension?"},{"key":"last_known_status","q":"Last known account status?"},{"key":"damages","q":"Charges/fees accrued?"},{"key":"evidence","q":"What can you attach?"}]},
 "card_decline_no_alert":{"label":"Card on file declined — no alert was ever sent","questions":[{"key":"account_number","q":"E-ZPass account number?"},{"key":"card_decline_date","q":"Approximate card decline date?"},{"key":"discovery","q":"How/when did you discover it?"},{"key":"fees_accrued","q":"Fees accrued?"},{"key":"evidence","q":"What can you attach?"}]},
}

STATE_CONTACTS={"NY":{"authority":"E-ZPass New York Service Center / TBTA Violations","address":"E-ZPass New York Customer Service Center\nP.O. Box 149003\nStaten Island, NY 10314-9003","violations_address":"TBTA Violations\nP.O. Box 15183\nAlbany, NY 12212-5183","response_deadline_days":30}}
free_use_tracker={}

def norm_plate(x): return re.sub(r"[^A-Z0-9]","",str(x or "").upper())
def norm_state(x): return str(x or "").upper()[:2]
def public_url(): return os.getenv("SCANPASS_PUBLIC_URL") or "https://scampass.onrender.com"
def get_supabase():
    url=os.getenv("SUPABASE_URL"); key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key or create_client is None: return None
    return create_client(url,key)

class LookupBody(BaseModel):
    plate:Optional[str]=None
    state:Optional[str]=None
class GenerateBody(BaseModel):
    free:Optional[bool]=False
    disputeType:str
    state:str
    plate:str
    fullName:Optional[str]=None
    address:Optional[str]=None
    email:Optional[str]=None
    answers:Dict[str,Any]={}
class EmailBody(BaseModel):
    email:Optional[str]=None
class DisputeCheckoutBody(BaseModel):
    email:Optional[str]=None
    dispute_id:Optional[str]=None

@scanpass_routes.get("/health")
def health():
    return {"ok":True,"service":"scanpass","phase":1,"states_live":SUPPORTED_STATES,"has_anthropic":bool(os.getenv("ANTHROPIC_API_KEY")),"has_stripe":bool(os.getenv("STRIPE_SECRET_KEY")),"has_supabase":bool(os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")))}

@scanpass_routes.post("/lookup")
def lookup(body:LookupBody, request:Request):
    plate,state=norm_plate(body.plate),norm_state(body.state)
    if not plate: return {"ok":False,"error":"Plate number is required."}
    if state not in SUPPORTED_STATES:
        return {"ok":False,"error":f"{state or 'That state'} is not live yet — Phase 1 supports NY. Join the waitlist to be alerted when your state goes live.","waitlist":True}
    result={"plate":plate,"state":state,"checked_at":datetime.utcnow().isoformat()+"Z","charges":[],"fees":[],"account_status":None,"notes":"Live portal scrape pending — connect n8n Plate Monitor to populate.","source":"stub"}
    sb=get_supabase()
    if sb:
        try:
            ip=(request.headers.get("x-forwarded-for","").split(",")[0].strip() or (request.client.host if request.client else None))
            sb.table("scanpass_lookups").insert({"plate_number":plate,"state":state,"ip":ip,"user_agent":request.headers.get("user-agent"),"found_charges":0,"raw_result":result}).execute()
        except Exception: pass
    return {"ok":True,"result":result}

@scanpass_routes.get("/dispute/types")
def dispute_types():
    return {"types":[{"key":k,"label":v["label"],"questions":v["questions"]} for k,v in DISPUTE_TYPES.items()]}

def free_ok(ip):
    day=datetime.utcnow().date().isoformat(); e=free_use_tracker.get(ip)
    if not e or e.get("day")!=day:
        free_use_tracker[ip]={"count":1,"day":day}; return True
    if e.get("count",0)>=1: return False
    e["count"]+=1; return True

def skeleton(data,meta,contact):
    today=datetime.utcnow().strftime("%B %d, %Y")
    return f"""{data.fullName or '[YOUR FULL NAME]'}
{data.address or '[YOUR ADDRESS]'}

{today}

{contact.get('violations_address') or contact.get('address')}

**Re: Formal Dispute — Plate {data.plate} ({data.state})**

To Whom It May Concern:

I am writing to formally dispute charges associated with the above-referenced license plate under the theory: *{meta['label']}*.

[The AI letter generator is offline — set ANTHROPIC_API_KEY to enable full drafting.]

Per applicable state law, I demand a written response within {contact.get('response_deadline_days',30)} days. Failure to respond will be construed as acceptance of this dispute.

Sincerely,

{data.fullName or '[YOUR FULL NAME]'}

---
**P.S. — If they don't respond:** file a complaint with the state Attorney General's consumer protection bureau, and if collections are involved, an FTC complaint at reportfraud.ftc.gov."""

def make_letter(data):
    meta=DISPUTE_TYPES.get(data.disputeType); contact=STATE_CONTACTS.get(norm_state(data.state))
    if not meta: raise HTTPException(status_code=400,detail="Unknown dispute type.")
    if not contact: raise HTTPException(status_code=400,detail=f"{data.state} is not live yet — Phase 1 is NY only.")
    if not os.getenv("ANTHROPIC_API_KEY") or Anthropic is None:
        return skeleton(data,meta,contact),"skeleton-no-api-key"
    qs="\n".join([f"- {q['q']}\n  ANSWER: {data.answers.get(q['key'],'(not provided)')}" for q in meta["questions"]])
    client=Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp=client.messages.create(model=MODEL,max_tokens=2000,system="Draft a complete, mailable toll-dispute letter in Markdown. Do not invent facts.",messages=[{"role":"user","content":f"DISPUTE: {meta['label']}\nSTATE: {data.state}\nPLATE: {data.plate}\nNAME: {data.fullName}\nADDRESS: {data.address}\nANSWERS:\n{qs}"}])
    return "\n".join([getattr(b,"text","") for b in resp.content if getattr(b,"type","")=="text"]),resp.model

@scanpass_routes.post("/dispute/generate")
def generate(data:GenerateBody, request:Request):
    ip=(request.headers.get("x-forwarded-for","").split(",")[0].strip() or (request.client.host if request.client else "unknown"))
    if data.free and not free_ok(ip):
        return {"ok":False,"error":"Free dispute already used. Upgrade for $4.99 per additional dispute, or $9.99/mo for unlimited monitoring + disputes.","paywall":True}
    letter,model=make_letter(data); dispute_id=None; persisted=False; sb=get_supabase()
    if sb:
        try:
            res=sb.table("scanpass_disputes").insert({"guest_email":data.email,"plate_number":norm_plate(data.plate),"state":norm_state(data.state),"dispute_type":data.disputeType,"status":"generated","inputs":data.answers,"letter_markdown":letter,"is_free":bool(data.free),"paid":bool(data.free)}).execute()
            dispute_id=res.data[0].get("id") if res.data else None; persisted=True
        except Exception: pass
    return {"ok":True,"letter":letter,"model":model,"dispute_id":dispute_id,"persisted":persisted}

@scanpass_routes.post("/stripe/subscribe")
def subscribe(body:EmailBody):
    stripe.api_key=os.getenv("STRIPE_SECRET_KEY"); price=os.getenv("STRIPE_MONITOR_PRICE_ID")
    if not stripe.api_key: raise HTTPException(status_code=500,detail="Stripe not configured.")
    if not price: raise HTTPException(status_code=500,detail="STRIPE_MONITOR_PRICE_ID not set.")
    s=stripe.checkout.Session.create(mode="subscription",line_items=[{"price":price,"quantity":1}],customer_email=body.email or None,success_url=f"{public_url()}/?paid=1&session={{CHECKOUT_SESSION_ID}}",cancel_url=f"{public_url()}/?canceled=1",allow_promotion_codes=True,metadata={"product":"scanpass_monitor"})
    return {"ok":True,"url":s.url}

@scanpass_routes.post("/stripe/dispute-checkout")
def dispute_checkout(body:DisputeCheckoutBody):
    stripe.api_key=os.getenv("STRIPE_SECRET_KEY"); price=os.getenv("STRIPE_DISPUTE_PRICE_ID")
    if not stripe.api_key: raise HTTPException(status_code=500,detail="Stripe not configured.")
    if not price: raise HTTPException(status_code=500,detail="STRIPE_DISPUTE_PRICE_ID not set.")
    s=stripe.checkout.Session.create(mode="payment",line_items=[{"price":price,"quantity":1}],customer_email=body.email or None,success_url=f"{public_url()}/?paid=1&session={{CHECKOUT_SESSION_ID}}",cancel_url=f"{public_url()}/?canceled=1",metadata={"product":"scanpass_dispute","dispute_id":body.dispute_id or ""})
    return {"ok":True,"url":s.url}

async def scanpass_stripe_webhook(request:Request):
    stripe.api_key=os.getenv("STRIPE_SECRET_KEY"); secret=os.getenv("STRIPE_WEBHOOK_SECRET")
    payload=await request.body(); sig=request.headers.get("stripe-signature")
    if not stripe.api_key or not secret: raise HTTPException(status_code=400,detail="Stripe webhook not configured.")
    try: stripe.Webhook.construct_event(payload,sig,secret)
    except Exception as e: raise HTTPException(status_code=400,detail=f"Webhook Error: {str(e)}")
    return {"received":True}
