"""price hunter: paste a product link. H web agents read it, find promo codes for that shop and
TRY them in the cart, and check the same product, new, at trusted French retailers.
the cheapest safe deal wins.

run:  uv run server.py   (reads HAI_API_KEY from .env or the environment)
open: http://127.0.0.1:8765
"""

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from hai_agents import Client
from pydantic import BaseModel

ROOT = Path(__file__).parent
AGENT = os.environ.get("HUNT_AGENT", "h/web-surfer-flash")
AGENT_VIEW = "https://platform.eu.hcompany.ai/agent-view/"
DONE = {"completed", "failed", "timed_out", "interrupted"}
ENDED = ("found", "missing", "blocked", "failed")

# trusted retailers only. key: (name, site, dealabs slug, the seller name that counts as "sold by the shop")
SHOPS = {
    "amazon": ("Amazon", "https://www.amazon.fr", "amazon", "Amazon"),
    "fnac": ("Fnac", "https://www.fnac.com", "fnac", "Fnac"),
    "darty": ("Darty", "https://www.darty.com", "darty", "Darty"),
    "boulanger": ("Boulanger", "https://www.boulanger.com", "boulanger", "Boulanger"),
    "cdiscount": ("Cdiscount", "https://www.cdiscount.com", "cdiscount", "Cdiscount"),
    "leclerc": ("E.Leclerc", "https://www.e.leclerc", "leclerc", "E.Leclerc"),
    "ldlc": ("LDLC", "https://www.ldlc.com", "ldlc", "LDLC"),
}

# ---------------------------------------------------------------- schemas

PRODUCT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "the page is a product page and you could read it"},
        "blocked": {"type": "boolean"},
        "shop": {"type": "string", "description": "name of the shop, e.g. Fnac"},
        "title": {"type": "string"},
        "brand": {"type": "string"},
        "model": {"type": "string", "description": "exact model name / reference"},
        "variant": {"type": "string", "description": "color, capacity, size… everything that tells this exact version apart"},
        "ean": {"type": ["string", "null"], "description": "EAN / GTIN if printed on the page"},
        "price": {"type": ["number", "null"], "description": "price in euros of the offer on this page"},
        "condition": {"type": "string", "enum": ["new", "refurbished", "used", "unknown"]},
        "seller": {"type": "string"},
        "in_stock": {"type": "boolean"},
        "note": {"type": "string", "description": "one short sentence"},
    },
    "required": ["ok", "blocked", "shop", "title", "brand", "model", "variant", "price", "condition", "seller", "in_stock", "note"],
}

OFFER = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "the exact same product and variant, following the rules"},
        "blocked": {"type": "boolean", "description": "a captcha or anti-bot wall stopped you"},
        "title": {"type": "string"},
        "price": {"type": ["number", "null"], "description": "price in euros, shipping excluded"},
        "in_stock": {"type": "boolean"},
        "condition": {"type": "string", "enum": ["new", "refurbished", "used", "unknown"]},
        "seller": {"type": "string", "description": "who sells AND ships it, exactly as written on the page"},
        "sold_by_shop": {"type": "boolean", "description": "true only if the shop itself sells it, not a marketplace partner"},
        "same_variant": {"type": "boolean", "description": "same model AND same color / capacity / size as the reference"},
        "delivery": {"type": "string"},
        "url": {"type": "string"},
        "note": {"type": "string", "description": "one short sentence"},
    },
    "required": ["found", "blocked", "title", "price", "in_stock", "condition", "seller", "sold_by_shop", "same_variant", "url", "note"],
}

COUPONS = {
    "type": "object",
    "properties": {
        "codes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "label": {"type": "string", "description": "what it promises"},
                    "result": {"type": "string", "enum": ["accepted", "rejected", "login_required", "not_tested"],
                               "description": "what the cart said when you typed it"},
                    "total_after": {"type": ["number", "null"], "description": "cart total shown right after the code was accepted"},
                    "message": {"type": "string", "description": "the cart's message, short, as written"},
                },
                "required": ["code", "label", "result", "total_after", "message"],
            },
        },
        "cart_total_before": {"type": ["number", "null"], "description": "cart total with only this product, before any code"},
        "note": {"type": "string", "description": "one short sentence"},
    },
    "required": ["codes", "cart_total_before", "note"],
}

# ---------------------------------------------------------------- tasks

LIVE = "Think out loud in English, in short sentences: people are watching you live."

READ_TASK = f"""\
Open this product page: {{url}}
Dismiss any cookie banner (refuse if possible, otherwise accept).
Read the product exactly as sold on this page: shop, full title, brand, exact model, the variant
(color, capacity, size…), the EAN if it is printed, the price in euros, the condition, the seller
and whether it is in stock. Do not add to cart, do not log in. Answer as soon as you have it.
{LIVE}
"""

OFFER_TASK = f"""\
You check one shop for a cheaper, SAFE deal. Shop: {{name}} ({{site}}).
Reference product: "{{title}}" — brand {{brand}}, model {{model}}, variant {{variant}}{{ean}}.
Reference price: {{price}} €.

1. Go to {{site}}. Dismiss any cookie banner.
2. Search the product with the site's own search bar (try the EAN if there is one).
3. Open the listing of the EXACT same product and variant. Ignore accessories, other models,
   other colors or capacities.
4. Rules: {{rules}}
   On the product page read the price, the condition, who sells and ships it, the stock and
   the delivery. If the default offer breaks the rules, look on the same page for an offer
   that follows them ("vendu et expédié par {{seller}}").
5. Answer. found=true only if the offer follows the rules. Never add to cart, never log in.
If a captcha or a bot wall blocks you, answer blocked=true right away.
{LIVE}
"""

BRAND_TASK = f"""\
Reference product: "{{title}}" — brand {{brand}}, model {{model}}, variant {{variant}}.
Find the brand's OWN official online store for France (for example apple.com/fr, sony.fr,
samsung.com/fr, dyson.fr). Search it with Bing if you need to, but only open the brand's own
domain. If the brand does not sell this product online in France, answer found=false.
On the official store, open the same product and variant and read the price (new), stock and
delivery. seller = the brand, sold_by_shop = true. Never add to cart, never log in.
{LIVE}
"""

COUPON_TASK = f"""\
Promo codes found online often don't work. Your job: find codes for {{name}} AND try them for real.
Product: "{{title}}" at {{price}} €, page {{url}}.

1. Go to {{dealabs}}. Dismiss the cookie banner. Note the codes still valid today that could fit
   this product (skip the ones clearly for another category or brand). Reveal them if needed
   ("voir le code"); if a new tab opens, come back to Dealabs. Keep at most 3, best first.
2. Open {{url}}, add the product to the cart, open the cart. Write down the total.
3. Type each code in the cart's promo code field, one by one. For each, write what the cart
   says: accepted (and the new total), rejected (and the message), or login_required if the
   field only exists once logged in. Remove an accepted code before trying the next one.
4. Stop at the cart: never log in, never fill an address, never go to delivery or payment.
If Dealabs has nothing usable, answer with an empty list. Be quick. {LIVE}
"""

# ---------------------------------------------------------------- plumbing


def load_key() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("HAI_API_KEY="):
                os.environ.setdefault("HAI_API_KEY", line.split("=", 1)[1].strip().strip('"'))
    key = os.environ.get("HAI_API_KEY")
    if not key:
        raise SystemExit("HAI_API_KEY missing: put it in .env (see .env.example)")
    return key


KEY = load_key()
client = Client(api_key=KEY)
http = httpx.Client(headers={"Authorization": f"Bearer {KEY}"}, follow_redirects=True, timeout=30)
app = FastAPI()
hunts: dict[str, dict] = {}


def first_line(s: str | None, n: int = 140) -> str:
    s = (s or "").strip().split("\n")[0].strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def domain(u: str) -> str:
    h = urlparse(u if "://" in u else "https://" + u).hostname or ""
    return h.removeprefix("www.")


def shop_of(url: str) -> str | None:
    d = domain(url)
    return next((k for k, v in SHOPS.items() if d == domain(v[1]) or d.endswith("." + domain(v[1]))), None)


def new_slot(name: str, site: str) -> dict:
    return {"name": name, "site": site, "status": "starting", "session_id": None, "watch": None,
            "thought": "", "action": "", "steps": 0, "cost": 0.0, "shot_v": 0, "shot": None,
            "cursor": None, "answer": None, "started": None, "finished": None}


def drive(hunt: dict, st: dict, task: str, schema: dict, answer_by: float) -> dict:
    """run one session to the end, keeping its slot up to date. returns the answer ({} if none).
    after answer_by seconds of real work the agent is told to wrap up with what it has"""
    try:
        s = client.sessions.create_session(
            agent=AGENT, messages=task, overrides={"agent.answer_format": schema}, max_time_s=360, queue=True
        )
    except Exception as e:  # quota, network…
        st.update(status="failed", thought=f"could not start: {e}"[:160])
        return {}
    st.update(session_id=s.id, watch=AGENT_VIEW + s.id, status="queued")
    hunt["session_ids"].append(s.id)

    idx, forced = 0, False
    while not hunt["cancelled"]:
        if not forced and st["started"] and time.time() - st["started"] > answer_by:
            forced = True
            try:
                client.sessions.force_session_answer(s.id)
                st["thought"] = "Time's up, wrapping up with what I have."
            except Exception:
                pass
        try:
            ch = client.sessions.get_session_changes(s.id, from_index=idx, include_events=True, wait_for_seconds=10)
        except Exception:
            time.sleep(1)
            continue
        if ch is None:
            continue
        if ch.status in ("pending", "queued"):
            st["status"] = "queued"
        elif st["status"] == "queued":
            st.update(status="running", started=time.time())

        for ev in ch.new_events or []:
            d = ev.model_dump(mode="json").get("data") or {}
            kind = d.get("kind")
            if kind == "observation_event" and (d.get("image") or {}).get("source"):
                try:
                    r = http.get(d["image"]["source"])
                    if r.status_code == 200:
                        st["shot"] = r.content
                        st["shot_v"] += 1
                except Exception:
                    pass
            elif kind == "policy_event":
                st["thought"] = first_line(d.get("reasoning_content")) or st["thought"]
                reqs = d.get("tool_reqs") or []
                if reqs:
                    a = reqs[0]
                    st["action"] = a.get("tool_name", "")
                    args = a.get("args") or {}
                    if isinstance(args.get("x"), (int, float)) and isinstance(args.get("y"), (int, float)):
                        st["cursor"] = [args["x"], args["y"], st["steps"] + 1]
            elif ev.type == "MetricsUpdateEvent":
                m = d.get("metrics") or {}
                st["steps"] = m.get("steps", st["steps"])
                st["cost"] = m.get("total_cost", st["cost"])
        idx += len(ch.new_events or [])

        if isinstance(ch.answer, dict):
            st["answer"] = ch.answer
        if ch.finished_at or ch.status in DONE or (ch.status == "idle" and st.get("answer")):
            st["finished"] = time.time()
            return st.get("answer") or {}
    return {}


# ---------------------------------------------------------------- the hunt


def safe(hunt: dict, a: dict) -> str | None:
    """None if we would recommend this offer, else the reason we don't"""
    if not (a.get("found") or a.get("price")) or not isinstance(a.get("price"), (int, float)):
        return "not found"
    if a.get("same_variant") is False:
        return "other variant"
    if hunt["new_only"] and a.get("condition") != "new":
        return "not new"
    if hunt["official_only"] and not a.get("sold_by_shop"):
        return "third-party seller"
    if a.get("in_stock") is False:
        return "out of stock"
    return None


def run_offer(hunt: dict, key: str) -> None:
    st, ref = hunt["offers"][key], hunt["product"]["answer"]
    rules = []
    if hunt["new_only"]:
        rules.append("NEW only (no refurbished, no used, no open box).")
    if hunt["official_only"]:
        rules.append("Sold AND shipped by the shop itself, never by a marketplace partner or third-party seller.")
    fields = dict(title=ref.get("title"), brand=ref.get("brand"), model=ref.get("model"), variant=ref.get("variant"),
                  ean=f", EAN {ref['ean']}" if ref.get("ean") else "", price=ref.get("price"))
    if key == "brand":
        task = BRAND_TASK.format(**fields)
    else:
        name, site, _, seller = SHOPS[key]
        task = OFFER_TASK.format(name=name, site=site, seller=seller, rules=" ".join(rules) or "none.", **fields)
    a = drive(hunt, st, task, OFFER, answer_by=120)
    if hunt["cancelled"]:
        return
    if key == "brand" and a.get("url"):
        st["site"], st["name"] = a["url"], (ref.get("brand") or "Brand") + " (official)"
    if not a:
        st["status"] = "failed"
    elif a.get("blocked"):
        st["status"] = "blocked"
    elif (why := safe(hunt, a)) is None:
        st["status"] = "found"
    else:
        st["status"], st["rejected"] = "missing", why


def run_coupon(hunt: dict, which: str, name: str, slug: str | None, title: str, price: float, url: str) -> None:
    st = hunt["coupons"][which]
    dealabs = f"https://www.dealabs.com/codes-promo/{slug}" if slug else \
        f"https://www.dealabs.com/codes-promo, then open the page of {name} with the site search"
    a = drive(hunt, st, COUPON_TASK.format(name=name, dealabs=dealabs, title=title, price=price, url=url), COUPONS, answer_by=170)
    if hunt["cancelled"]:
        return
    # only a code the cart accepted counts. the saving is read from the cart, not from the ad
    before = a.get("cart_total_before") or price
    best = None
    for c in a.get("codes") or []:
        t = c.get("total_after")
        if c.get("result") == "accepted" and isinstance(t, (int, float)) and t < before:
            if best is None or t < best["total_after"]:
                best = c
    st["best"] = {"code": best["code"], "price": round(price - (before - best["total_after"]), 2),
                  "saving": round(before - best["total_after"], 2)} if best else None
    st["status"] = "found" if best else ("missing" if a else "failed")


def orchestrate(hunt: dict) -> None:
    # 1. read the link
    p = hunt["product"]
    ref = drive(hunt, p, READ_TASK.format(url=hunt["url"]), PRODUCT, answer_by=90)
    if hunt["cancelled"]:
        hunt["phase"] = "done"
        return
    if not ref.get("ok") or not isinstance(ref.get("price"), (int, float)):
        p["status"] = "blocked" if ref.get("blocked") else "failed"
        hunt["phase"] = "done"
        return
    p["status"] = "found"
    p["name"] = ref.get("shop") or p["name"]
    if ref.get("ean") and not any(ch.isdigit() for ch in ref["ean"]):
        ref["ean"] = None  # "not shown on the page"

    # 2. codes for this shop (tried in the cart) + the same product at trusted shops, all at once
    hunt["phase"] = "hunt"
    src = shop_of(hunt["url"])
    hunt["coupons"]["source"] = new_slot(p["name"], hunt["url"])
    keys = [k for k in SHOPS if k != src] + ["brand"]
    for k in keys:
        hunt["offers"][k] = new_slot(*((SHOPS[k][0], SHOPS[k][1]) if k != "brand" else (f"{ref.get('brand') or 'Brand'} (official)", "")))
    threads = [threading.Thread(target=run_coupon, daemon=True, args=(
        hunt, "source", p["name"], SHOPS[src][2] if src else None, ref["title"], ref["price"], hunt["url"]))]
    threads += [threading.Thread(target=run_offer, args=(hunt, k), daemon=True) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if hunt["cancelled"]:
        hunt["phase"] = "done"
        return

    # 3. the best safe alternative beats the link (tested code included)? try codes there too
    src_best = (hunt["coupons"]["source"].get("best") or {}).get("price") or ref["price"]
    alts = sorted((k for k, s in hunt["offers"].items() if s["status"] == "found" and k in SHOPS),
                  key=lambda k: hunt["offers"][k]["answer"]["price"])
    # not worth a third round for a few cents
    if alts and hunt["offers"][alts[0]]["answer"]["price"] < src_best * 0.99:
        k = alts[0]
        a = hunt["offers"][k]["answer"]
        hunt["phase"] = "alt_codes"
        hunt["coupons"]["alt"] = new_slot(SHOPS[k][0], SHOPS[k][1])
        hunt["coupons"]["alt"]["for"] = k
        run_coupon(hunt, "alt", SHOPS[k][0], SHOPS[k][2], a.get("title") or ref["title"], a["price"], a.get("url") or SHOPS[k][1])
    hunt["phase"] = "done"


def public(hunt: dict) -> dict:
    clean = lambda s: {x: v for x, v in s.items() if x != "shot"}
    return {"id": hunt["id"], "url": hunt["url"], "phase": hunt["phase"],
            "new_only": hunt["new_only"], "official_only": hunt["official_only"],
            "product": clean(hunt["product"]),
            "offers": {k: clean(s) for k, s in hunt["offers"].items()},
            "coupons": {k: clean(s) for k, s in hunt["coupons"].items()}}


def slot(hunt: dict, bucket: str, key: str) -> dict | None:
    if bucket == "product":
        return hunt["product"]
    return hunt.get(bucket, {}).get(key) if bucket in ("offers", "coupons") else None


# ---------------------------------------------------------------- http


class HuntIn(BaseModel):
    url: str
    new_only: bool = True
    official_only: bool = True


@app.post("/api/hunt")
def start(body: HuntIn):
    url = body.url.strip()
    if "://" not in url:
        url = "https://" + url
    if "." not in domain(url):
        raise HTTPException(400, "Paste a product page link.")
    hid = uuid.uuid4().hex[:10]
    hunt = {"id": hid, "url": url, "created": time.time(), "cancelled": False, "session_ids": [],
            "new_only": body.new_only, "official_only": body.official_only, "phase": "read",
            "product": new_slot(domain(url), url), "offers": {}, "coupons": {}}
    hunts[hid] = hunt
    threading.Thread(target=orchestrate, args=(hunt,), daemon=True).start()
    return {"id": hid}


@app.get("/api/hunt/{hid}/stream")
async def stream(hid: str):
    hunt = hunts.get(hid)
    if not hunt:
        raise HTTPException(404)

    async def gen():
        last = None
        while True:
            snap = json.dumps(public(hunt), default=str)
            if snap != last:
                yield f"data: {snap}\n\n"
                last = snap
            if hunt["phase"] == "done":
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/hunt/{hid}/{bucket}/{key}/shot")
def shot(hid: str, bucket: str, key: str):
    s = slot(hunts[hid], bucket, key) if hid in hunts else None
    if not s or not s.get("shot"):
        raise HTTPException(404)
    return Response(s["shot"], media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/hunt/{hid}/stop")
def stop(hid: str):
    hunt = hunts.get(hid)
    if not hunt:
        raise HTTPException(404)
    hunt["cancelled"] = True
    for sid in hunt["session_ids"]:
        try:
            client.sessions.cancel_session(sid)
        except Exception:
            pass
    for s in [hunt["product"], *hunt["offers"].values(), *hunt["coupons"].values()]:
        if s["status"] not in ENDED:
            s["status"], s["thought"] = "failed", "stopped"
    hunt["phase"] = "done"
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8765)), log_level="warning")
