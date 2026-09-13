"""
LINE Console API — FastAPI บน Vercel Python runtime
ทุก endpoint (ยกเว้น /api/webhook, /api/health) ต้องมี  Authorization: Bearer <LIFF id_token>
"""
import asyncio
import base64
import copy
import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from _lib import line, supa
from _lib.auth import current_admin
from _lib.config import (LINE_CHANNEL_SECRET, CRON_SECRET, ALERT_USER_IDS,
                         LINE_CHANNEL_ACCESS_TOKEN, LIFF_CHANNEL_ID,
                         SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                         LINE_LOGIN_CHANNEL_TOKEN, LINE_LOGIN_CHANNEL_ID,
                         LINE_LOGIN_CHANNEL_SECRET, LINE_LOGIN_ASSERTION_KID,
                         REG_LIFF_CHANNEL_ID,
                         LINE_LOGIN_ASSERTION_PRIVATE_KEY, APP_URL, EASYSLIP_TOKEN,
                         ENFORCE_RICHMENU_ID, ENFORCE_EXCLUDE_MENUS,
                         GEMINI_API_KEY, GEMINI_MODEL, GEMINI_TRIGGER_MENU_NAME,
                         ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
                         SLIP_SUCCESS_RICHMENU_ID)
import anthropic
import httpx

app = FastAPI(title="LINE Console API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

NOW = lambda: dt.datetime.now(dt.timezone.utc).isoformat()


# ============================================================
# rate limit (in-memory, ต่อ instance) + alert
# ============================================================
_hits: dict[str, list[float]] = {}


def _rate_limit(key: str, limit: int, window: float):
    now = time.time()
    q = _hits.setdefault(key, [])
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        raise HTTPException(429, "เรียกถี่เกินไป ลองใหม่อีกครั้ง")
    q.append(now)


def _rate_ok(key: str, limit: int, window: float) -> bool:
    """เหมือน _rate_limit แต่คืน False แทนการ raise — ใช้ในลูปที่ไม่อยากให้ 1 คนล้มทั้ง batch"""
    now = time.time()
    q = _hits.setdefault(key, [])
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now)
    return True


_alert_last: dict[str, float] = {}


async def alert_admin(subject: str, detail: str = "", throttle_key: str | None = None):
    """แจ้ง error เข้า LINE ของแอดมิน (throttle 10 นาที/key)"""
    if not ALERT_USER_IDS:
        print("ALERT:", subject, detail)
        return
    k = throttle_key or subject
    if time.time() - _alert_last.get(k, 0) < 600:
        return
    _alert_last[k] = time.time()
    text = f"⚠️ LINE Console\n{subject}"
    if detail:
        text += f"\n\n{detail[:400]}"
    for uid in ALERT_USER_IDS:
        try:
            await line.push(uid, [{"type": "text", "text": text}])
        except Exception:
            pass


# ============================================================
# health / me
# ============================================================
@app.api_route("/r/{code}", methods=["GET"])
async def short_redirect(code: str, request: Request):
    from fastapi.responses import RedirectResponse
    rows = await supa.select("short_links", params={"code": f"eq.{code}", "select": "*", "limit": "1"})
    if not rows:
        return RedirectResponse(APP_URL, status_code=302)
    sl = rows[0]
    try:
        await supa.insert("link_clicks", {
            "code": code, "target": sl["target"], "broadcast_id": sl.get("broadcast_id"),
            "line_user_id": request.query_params.get("u"),
        })
        await supa.update("short_links", {"clicks": (sl.get("clicks") or 0) + 1}, {"code": f"eq.{code}"})
    except Exception:
        pass
    return RedirectResponse(sl["target"], status_code=302)


@app.get("/api/links")
async def links_list(admin=Depends(current_admin)):
    return {"links": await supa.select("short_links", params={
        "select": "*", "order": "created_at.desc", "limit": "100"})}


@app.post("/api/links")
async def link_create(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    target = (b.get("target") or "").strip()
    if not re.match(r"^https?://", target):
        raise HTTPException(400, "target ต้องเป็น URL")
    code = b.get("code") or os.urandom(4).hex()[:7]
    await supa.upsert("short_links", {
        "code": code, "target": target, "label": b.get("label"),
        "broadcast_id": b.get("broadcastId"), "created_by": admin["userId"],
    }, on_conflict="code")
    return {"ok": True, "code": code, "shortUrl": f"{APP_URL}/r/{code}"}


@app.get("/api/links/{code}/stats")
async def link_stats(code: str, admin=Depends(current_admin)):
    clicks = await supa.select("link_clicks", params={
        "code": f"eq.{code}", "select": "clicked_at,line_user_id", "order": "clicked_at.desc", "limit": "1000"})
    by_day: dict[str, int] = {}
    for c in clicks:
        by_day[c["clicked_at"][:10]] = by_day.get(c["clicked_at"][:10], 0) + 1
    return {"total": len(clicks), "unique_users": len({c["line_user_id"] for c in clicks if c["line_user_id"]}),
            "by_day": [{"day": k, "clicks": v} for k, v in sorted(by_day.items())]}


@app.delete("/api/links/{code}")
async def link_delete(code: str, admin=Depends(current_admin)):
    await supa.delete("short_links", {"code": f"eq.{code}"})
    return {"ok": True}


@app.get("/api/health")
async def health(deep: int = 0, test_gemini: int = 0, test_uid: str = "", test_ask: str = ""):
    out = {"ok": True, "time": NOW(),
           "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "?")[:7],
           "deployed_at": os.environ.get("VERCEL_DEPLOYMENT_ID", "?")}
    if not deep:
        return out
    # deep check
    checks = {}
    checks["env"] = {
        "line_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "line_secret": bool(LINE_CHANNEL_SECRET),
        "liff_channel_id": bool(LIFF_CHANNEL_ID),
        "supabase": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
        "alerts": bool(ALERT_USER_IDS),
    }
    try:
        await supa.count("admins")
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"; out["ok"] = False
    try:
        q = await line.message_quota()
        checks["line_quota"] = q.get("quota", {}).get("quota", q.get("quota"))
        checks["line_usage"] = q.get("totalUsage")
    except Exception as e:
        checks["line_quota"] = f"error: {e}"; out["ok"] = False
    try:
        rows = await supa.select("operations", params={
            "select": "action,created_at", "action": "like.*cron*",
            "order": "created_at.desc", "limit": "5"})
        checks["last_cron"] = rows
    except Exception:
        pass
    # เช็คว่า migration 0002/0003/0004 รันครบใน DB ไหม
    checks["schema"] = {}
    for tb in ("slips", "payment_accounts", "postback_actions", "app_settings",
               "richmenu_history", "registrations", "webhook_jobs", "tasks",
               "contact_notes", "classes", "attendance", "kb_articles", "postback_clicks"):
        try:
            n = await supa.count(tb)
            checks["schema"][tb] = f"ok ({n} rows)"
        except Exception as e:
            checks["schema"][tb] = f"MISSING — {str(e)[:100]}"
            out["ok"] = False
    try:
        await supa.select("rich_menus", params={"select": "gemini_enabled,gemini_temperature", "limit": "1"})
        checks["schema"]["rich_menus.gemini_*"] = "ok"
    except Exception as e:
        checks["schema"]["rich_menus.gemini_*"] = f"MISSING — {str(e)[:100]}"
        out["ok"] = False
    # EasySlip
    if EASYSLIP_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=12) as _c:
                _r = await _c.get("https://developer.easyslip.com/api/v1/me",
                                  headers={"Authorization": f"Bearer {EASYSLIP_TOKEN}"})
            _j = _r.json()
            if _r.status_code == 200:
                _q = (_j.get("data") or {}).get("quota") or _j.get("data") or {}
                _used = _q.get("usedQuota", _q.get("used"))
                _max = _q.get("maxQuota", _q.get("max", _q.get("limit")))
                _remain = _q.get("remainingQuota")
                if _used is None and _max is None:
                    # เผื่อ EasySlip เปลี่ยนชื่อ field อีก — โชว์ raw ไว้ดูเอง แทนที่จะขึ้น ?/? เฉยๆ
                    checks["easyslip"] = f"ok — quota (raw): {str(_q)[:150]}"
                else:
                    prefix = "⚠️ QUOTA หมด — " if _remain == 0 else "ok — "
                    reset_at = _q.get("expiredAt")
                    suffix = f" (รีเซ็ต {reset_at})" if reset_at else ""
                    checks["easyslip"] = f"{prefix}quota ใช้ {_used}/{_max}{suffix}"
            else:
                checks["easyslip"] = f"token error {_r.status_code}: {str(_j)[:120]}"
        except Exception as e:
            checks["easyslip"] = f"error: {str(e)[:120]}"
    else:
        checks["easyslip"] = "off (ยังไม่ตั้ง EASYSLIP_TOKEN)"

    # LIFF sync
    try:
        _tok, _terr = await _liff_channel_token()
        if _terr:
            checks["liff_sync"] = f"error: {_terr[:150]}"
        elif _tok:
            async with httpx.AsyncClient(timeout=12) as _c:
                _r = await _c.get("https://api.line.me/liff/v1/apps",
                                  headers={"Authorization": f"Bearer {_tok}"})
            checks["liff_sync"] = (f"ok ({len(_r.json().get('apps', []))} apps)"
                                   if _r.status_code == 200 else f"{_r.status_code}: {_r.text[:100]}")
        else:
            checks["liff_sync"] = "off (ยังไม่ตั้ง LINE_LOGIN_CHANNEL_SECRET / KID)"
    except Exception as e:
        checks["liff_sync"] = f"error: {str(e)[:120]}"
    checks["gemini"] = {
        "provider": "anthropic",  # เปลี่ยนมาจาก Gemini — ชื่อ key นี้คงไว้เพื่อ compat กับของเดิม
        "api_key_set": bool(ANTHROPIC_API_KEY),
        "model": ANTHROPIC_MODEL,
        "trigger_menu_name": GEMINI_TRIGGER_MENU_NAME,
    }
    try:
        rows = await supa.select("rich_menus", params={
            "select": "rich_menu_id,name", "name": f"eq.{GEMINI_TRIGGER_MENU_NAME}"})
        checks["gemini"]["matching_menu_ids"] = [r["rich_menu_id"] for r in rows]
        all_names = await supa.select("rich_menus", params={"select": "name"})
        checks["gemini"]["all_menu_names_in_cache"] = [r["name"] for r in all_names]
    except Exception as e:
        checks["gemini"]["error"] = str(e)

    if test_uid:
        try:
            rid = await line.user_richmenu_get(test_uid)
            checks["gemini"]["test_uid"] = test_uid
            checks["gemini"]["test_uid_live_rich_menu_id"] = rid
            checks["gemini"]["test_uid_matches_trigger"] = rid in set(checks["gemini"].get("matching_menu_ids") or [])
        except Exception as e:
            checks["gemini"]["test_uid_error"] = str(e)

    if test_gemini:
        try:
            _cl = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            resp = await _cl.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=200,
                messages=[{"role": "user", "content": "สวัสดี ทดสอบระบบ"}])
            checks["gemini"]["live_test_status"] = 200
            checks["gemini"]["live_test_body"] = "".join(
                b.text for b in resp.content if b.type == "text")[:1000]
        except anthropic.APIStatusError as e:
            checks["gemini"]["live_test_status"] = e.status_code
            checks["gemini"]["live_test_body"] = str(e.message)[:1000]
        except Exception as e:
            checks["gemini"]["live_test_error"] = str(e)

    if test_ask:
        try:
            ctx = await _gemini_context()
            kb = await _gemini_retrieve(test_ask)
            ans = await _gemini_answer(test_ask, context=(ctx + ("\n\n" + kb if kb else "")),
                                       uid=(test_uid or None), user_facts="(ทดสอบระบบ)")
            checks["gemini"]["ask_q"] = test_ask
            checks["gemini"]["ask_kb_hit"] = bool(kb)
            checks["gemini"]["ask_answer"] = ans
            if ans is None:
                # debug: ยิงตรงดูว่า model คืนอะไร (ไม่ใช้ tools กันเคส tool loop ตัน)
                try:
                    _cl = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
                    dr = await _cl.messages.create(
                        model=ANTHROPIC_MODEL, max_tokens=1024,
                        messages=[{"role": "user", "content": test_ask}])
                    checks["gemini"]["ask_debug"] = dr.to_json()[:800]
                except Exception as e2:
                    checks["gemini"]["ask_debug"] = f"debug call error: {e2}"
        except Exception as e:
            checks["gemini"]["ask_error"] = repr(e)

    out["checks"] = checks
    return out


@app.get("/api/me")
async def me(admin=Depends(current_admin)):
    return admin


@app.get("/api/search")
async def global_search(admin=Depends(current_admin), q: str = ""):
    q = (q or "").strip()
    if len(q) < 2:
        return {"users": [], "registrations": [], "tasks": []}
    like = f"*{q}*"
    users = await supa.select("line_users", params={
        "select": "line_user_id,display_name,picture_url,tags,stage,is_following",
        "or": f"(display_name.ilike.{like},line_user_id.ilike.{like},note.ilike.{like})", "limit": "20"})
    regs = await supa.select("registrations", params={
        "select": "id,line_user_id,name,tel,email,org,course_raw,paid",
        "or": f"(name.ilike.{like},tel.ilike.{like},email.ilike.{like},org.ilike.{like})", "limit": "20"})
    tasks = []
    try:
        tasks = await supa.select("tasks", params={
            "select": "id,title,status,line_user_id,due_at", "title": f"ilike.{like}", "limit": "20"})
    except Exception:
        pass
    return {"users": users, "registrations": regs, "tasks": tasks}


# ============================================================
# webhook — รับ event จาก LINE (ไม่ต้อง auth, verify signature)
# ============================================================
@app.post("/api/webhook")
async def webhook(request: Request):
    ip = request.headers.get("x-forwarded-for", "?").split(",")[0].strip()
    _rate_limit(f"wh:{ip}", limit=120, window=60)  # 120 req/นาที/ip (LINE ยิงเป็น batch อยู่แล้ว)
    body = await request.body()
    sig = request.headers.get("x-line-signature", "")
    if LINE_CHANNEL_SECRET:
        mac = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(base64.b64encode(mac).decode(), sig):
            raise HTTPException(403, "bad signature")
    elif sig:
        pass  # ยังไม่ตั้ง secret

    data = json.loads(body or "{}")
    events = data.get("events", [])
    # ข้าม event ที่ LINE ส่งซ้ำ (redelivery) — เราประมวลผลรอบแรกไปแล้ว
    events = [e for e in events if not e.get("deliveryContext", {}).get("isRedelivery")]

    # แสดง "กำลังพิมพ์…" ทันทีสำหรับ message/postback (ไม่ block)
    loading_uids = {e.get("source", {}).get("userId") for e in events
                    if e.get("type") in ("message", "postback")
                    and e.get("source", {}).get("type") == "user"
                    and e.get("source", {}).get("userId")}
    if loading_uids:
        await asyncio.gather(*[line.start_loading(u, 20) for u in loading_uids], return_exceptions=True)
    rows_ev, user_patches, follow_rows, in_msgs = [], {}, [], []
    follow_uids, unfollow_uids = [], []
    bc_clicks = []   # คลิก postback ที่มาจาก broadcast (มีรหัสแนบท้าย data)

    for ev in events:
        et = ev.get("type")
        src = ev.get("source", {})
        uid = src.get("userId")
        msg = ev.get("message", {})
        pb = ev.get("postback", {})
        if et == "postback" and pb.get("data"):
            # แกะรหัส broadcast ที่แนบท้าย data ออกก่อน (ถ้ามี) แล้วแทนที่ด้วย data สะอาด
            # -> downstream (postback_actions/auto_reply matching, events log) เห็นแต่ data เดิมเป๊ะ ไม่กระทบ match
            clean, bcid = _split_postback_data(pb["data"])
            if bcid:
                pb["data"] = clean
                if uid:
                    bc_clicks.append({"broadcast_id": bcid, "line_user_id": uid, "data": clean})
        ts_ms = ev.get("timestamp")
        event_ts = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).isoformat() if ts_ms else NOW()
        dc = ev.get("deliveryContext", {})

        rows_ev.append({
            "event_type": et, "line_user_id": uid,
            "message_type": msg.get("type"), "text": msg.get("text") or pb.get("data"),
            "reply_token": ev.get("replyToken"),
            "postback_data": pb.get("data"),
            "webhook_event_id": ev.get("webhookEventId"),
            "event_ts": event_ts,
            "is_redelivery": bool(dc.get("isRedelivery")),
            "payload": ev,
        })
        if not uid:
            continue

        if et == "follow":
            follow_uids.append(uid)
            is_unblocked = bool(ev.get("follow", {}).get("isUnblocked"))
            follow_rows.append({"line_user_id": uid, "action": "follow",
                                "is_unblocked": is_unblocked, "event_ts": event_ts})
            user_patches[uid] = {
                "line_user_id": uid, "is_following": True, "unfollowed_at": None,
                "followed_at": event_ts, "last_event_at": event_ts,
                "source": "webhook", "updated_at": NOW(),
            }
        elif et == "unfollow":
            unfollow_uids.append(uid)
            follow_rows.append({"line_user_id": uid, "action": "unfollow", "event_ts": event_ts})
            user_patches[uid] = {
                "line_user_id": uid, "is_following": False,
                "unfollowed_at": event_ts, "last_event_at": event_ts,
                "source": "webhook", "updated_at": NOW(),
            }
        else:
            user_patches.setdefault(uid, {
                "line_user_id": uid, "is_following": True,
                "last_event_at": event_ts, "source": "webhook", "updated_at": NOW(),
            })

        # ---- inbox: บันทึกข้อความเข้า ----
        if et == "message":
            mt = msg.get("type")
            preview = msg.get("text") or {"image": "[รูปภาพ]", "video": "[วิดีโอ]", "audio": "[เสียง]",
                                          "file": f"[ไฟล์] {msg.get('fileName', '')}", "location": "[ตำแหน่ง]",
                                          "sticker": "[สติกเกอร์]"}.get(mt, f"[{mt}]")
            in_msgs.append({"line_user_id": uid, "direction": "in", "by": "user",
                            "msg_type": mt, "text": msg.get("text"), "payload": msg,
                            "created_at": event_ts})
            up = user_patches.get(uid, {"line_user_id": uid, "updated_at": NOW()})
            up["last_message_at"] = event_ts
            up["last_message_text"] = preview[:200]
            up["unread"] = None  # จะ increment ทีหลัง
            user_patches[uid] = up

    try:
        if rows_ev:
            await supa.insert("webhook_events", rows_ev)
        if in_msgs:
            await supa.insert("messages", in_msgs)
        if follow_rows:
            await supa.insert("follow_history", follow_rows)  # noqa
        if bc_clicks:
            try:
                await supa.insert("postback_clicks", bc_clicks)
            except Exception as e:
                print("postback click log error (migration 0012 รันหรือยัง?):", e)

        # increment unread สำหรับคนที่ทักเข้ามา
        for uid in {m["line_user_id"] for m in in_msgs}:
            ex = await supa.select("line_users", params={
                "select": "unread", "line_user_id": f"eq.{uid}", "limit": "1"})
            cur_un = (ex[0].get("unread") if ex else 0) or 0
            n = sum(1 for m in in_msgs if m["line_user_id"] == uid)
            user_patches[uid]["unread"] = cur_un + n

        # อัปเดต counter + first_followed_at (ต้องอ่านค่าเดิมก่อน)
        touched = set(follow_uids) | set(unfollow_uids)
        if touched:
            existing = {r["line_user_id"]: r for r in await supa.select("line_users", params={
                "select": "line_user_id,follow_count,block_count,first_followed_at",
                "line_user_id": f"in.({','.join(touched)})", "limit": "1000",
            })}
            for uid in touched:
                old = existing.get(uid, {})
                p = user_patches[uid]
                if uid in follow_uids:
                    p["follow_count"] = (old.get("follow_count") or 0) + 1
                    if not old.get("first_followed_at"):
                        p["first_followed_at"] = p["followed_at"]
                if uid in unfollow_uids:
                    p["block_count"] = (old.get("block_count") or 0) + 1

        if user_patches:
            await supa.upsert("line_users", list(user_patches.values()), on_conflict="line_user_id")
    except Exception as e:
        print("webhook store error:", e)
        await alert_admin("webhook เก็บข้อมูลไม่สำเร็จ", str(e), "wh_store")

    # ---- PDPA: คำสั่งขอหยุด/รับข่าวสาร ----
    consent_uids: set = set()
    try:
        consent_uids = await _handle_consent_keywords(events)
    except Exception as e:
        print("consent kw error:", e)
    if consent_uids:
        events = [e for e in events if e.get("source", {}).get("userId") not in consent_uids]

    # ---- ดึงโปรไฟล์คนที่เพิ่ง follow (จังหวะที่ดีที่สุด) ----
    if follow_uids:
        try:
            patch = []
            for uid in list(set(follow_uids))[:20]:
                p = await line.get_profile(uid)
                if p:
                    patch.append({
                        "line_user_id": uid,
                        "display_name": _clean_str(p.get("displayName")),
                        "picture_url": _clean_str(p.get("pictureUrl")),
                        "status_message": _clean_str(p.get("statusMessage")),
                        "language": _clean_str(p.get("language")),
                        "updated_at": NOW(),
                    })
            if patch:
                await supa.upsert("line_users", patch, on_conflict="line_user_id")
        except Exception as e:
            print("follow profile fetch error:", e)

    # ---- หา user ที่อยู่บนเมนูที่เปิด Gemini (เช็ค rich menu สดทีละคน) ----
    # Gemini เป็น "ตัวสำรอง" เท่านั้น — กฎคีย์เวิร์ด/ปุ่ม/ต้อนรับ ทำงานปกติเสมอ
    # Gemini จะตอบเฉพาะ "ข้อความที่ไม่ตรงกฎไหนเลย" (แทน fallback) ของ user บนเมนู Gemini
    gemini_uids: set = set()
    text_uids = {e["source"]["userId"] for e in events
                 if e.get("type") == "message" and e.get("message", {}).get("type") == "text"
                 and e.get("replyToken") and e.get("source", {}).get("userId")}
    try:
        gmenu_ids = await _gemini_enabled_menu_ids() if text_uids else set()
        if gmenu_ids:
            async def _chk(u):
                try:
                    return u, await line.user_richmenu_get(u)
                except Exception:
                    return u, None
            for u, rid in await asyncio.gather(*[_chk(u) for u in text_uids]):
                if rid in gmenu_ids:
                    gemini_uids.add(u)
    except BaseException as e:  # noqa: BLE001
        print("gemini menu check error:", repr(e))

    # ---- auto-reply ก่อน (คีย์เวิร์ด/ปุ่ม/ต้อนรับ) — ข้าม fallback ให้ user บนเมนู Gemini ----
    replied_uids: set = set()
    try:
        replied_uids = await asyncio.wait_for(
            _handle_auto_replies(events, gemini_uids=gemini_uids), timeout=12) or set()
    except BaseException as e:  # noqa: BLE001 — webhook ต้องตอบ 200 เสมอ
        print("auto-reply error:", repr(e))

    # ---- Gemini: ตอบข้อความที่ไม่ตรงกฎ ของ user บนเมนู Gemini ----
    if gemini_uids:
        try:
            g_targets = [e for e in events
                         if e.get("type") == "message" and e.get("message", {}).get("type") == "text"
                         and e.get("source", {}).get("userId") in gemini_uids
                         and e.get("source", {}).get("userId") not in replied_uids]
            if g_targets:
                await asyncio.wait_for(_handle_gemini_replies(g_targets), timeout=28)
        except BaseException as e:  # noqa: BLE001
            print("gemini reply error:", repr(e))

    # ---- automation: trigger=follow ----
    if follow_uids:
        try:
            await _enroll_automations("follow", list(set(follow_uids)))
        except Exception as e:
            print("automation enroll error:", e)

    # ---- สลิปโอนเงิน: เข้าคิว webhook_jobs แล้วประมวลผลนอก request (webhook ตอบเร็ว ไม่ timeout) ----
    img_events = [e for e in events if e.get("type") == "message"
                  and e.get("message", {}).get("type") in ("image", "file")]
    if img_events:
        queued = False
        try:
            await supa.insert("webhook_jobs", {"kind": "slips", "payload": {"events": img_events}})
            queued = True
        except Exception as e:
            print("webhook_jobs insert failed, processing inline:", e)
        if queued:
            await _fire_and_forget("/api/internal/run-jobs")  # กระตุ้นให้รันทันที (cron เป็น backup)
        else:
            try:
                await asyncio.wait_for(_handle_slips(img_events), timeout=25)
            except BaseException as e:
                print("slip handler error:", repr(e))
    return {"ok": True}


async def _fire_and_forget(path: str):
    """ยิง request ไปที่ตัวเอง โดยไม่รอผล — ให้ invocation ใหม่ทำงานหนักแทน (budget 60s ของตัวเอง)"""
    url = f"{APP_URL}{path}" + ("?" if "?" not in path else "&") + f"key={CRON_SECRET}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=2.0)) as c:
            await c.post(url)
    except Exception:
        pass  # ไม่รอผล — ถ้าไม่ถึงก็มี cron 5 นาที drain ให้


async def _run_webhook_jobs(limit: int = 15) -> dict:
    try:
        jobs = await supa.select("webhook_jobs", params={
            "select": "*", "status": "eq.pending", "order": "created_at.asc", "limit": str(limit)})
    except Exception as e:
        return {"error": str(e)[:120], "ran": 0}
    done = failed = 0
    for j in jobs:
        await supa.update("webhook_jobs", {"attempts": (j.get("attempts") or 0) + 1},
                          {"id": f"eq.{j['id']}"})
        try:
            if j["kind"] == "slips":
                evs = (j.get("payload") or {}).get("events") or []
                await asyncio.wait_for(_handle_slips(evs), timeout=45)
            await supa.update("webhook_jobs", {"status": "done", "processed_at": NOW()},
                              {"id": f"eq.{j['id']}"})
            done += 1
        except Exception as e:
            st = "failed" if (j.get("attempts") or 0) + 1 >= 3 else "pending"
            await supa.update("webhook_jobs", {"status": st, "error": str(e)[:200]},
                              {"id": f"eq.{j['id']}"})
            failed += 1
    return {"ran": len(jobs), "done": done, "failed": failed}


@app.api_route("/api/internal/run-jobs", methods=["GET", "POST"])
@app.api_route("/api/cron/run-jobs", methods=["GET", "POST"])
async def run_jobs_endpoint(request: Request):
    _check_cron_key(request)
    return {"ok": True, **await _run_webhook_jobs()}


_OPTOUT_KW = ("ยกเลิกรับข่าว", "หยุดส่งข่าว", "หยุดส่งข้อความ", "ไม่รับข่าวสาร", "unsubscribe", "เลิกรับข่าว", "ขอหยุดรับข่าว")
_OPTIN_KW = ("รับข่าวสาร", "สมัครรับข่าว", "subscribe", "เปิดรับข่าว")


async def _handle_consent_keywords(events: list) -> set:
    handled: set = set()
    for e in events:
        if e.get("type") != "message" or e.get("message", {}).get("type") != "text":
            continue
        uid = e.get("source", {}).get("userId")
        rtok = e.get("replyToken")
        if not uid:
            continue
        txt = (e["message"].get("text") or "").lower().strip()
        if any(k in txt for k in _OPTOUT_KW):
            handled.add(uid)
            await supa.update("line_users", {"consent": False, "consent_at": NOW(), "updated_at": NOW()},
                              {"line_user_id": f"eq.{uid}"})
            if rtok:
                await line.reply(rtok, [{"type": "text", "text":
                    "รับทราบครับ ระบบจะไม่ส่งข่าวสาร/โปรโมชันให้อีก\n(ยังตอบข้อความที่คุณถามเข้ามาตามปกติ) "
                    "พิมพ์ 'รับข่าวสาร' เพื่อเปิดรับใหม่"}])
        elif any(k in txt for k in _OPTIN_KW):
            handled.add(uid)
            await supa.update("line_users", {"consent": True, "consent_at": NOW(), "updated_at": NOW()},
                              {"line_user_id": f"eq.{uid}"})
            if rtok:
                await line.reply(rtok, [{"type": "text", "text": "เปิดรับข่าวสารเรียบร้อยครับ 🙏"}])
    return handled


async def _get_setting(key: str, default=None):
    try:
        rows = await supa.select("app_settings", params={"key": f"eq.{key}", "select": "value", "limit": "1"})
        return rows[0]["value"] if rows else default
    except Exception:
        return default


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


async def _easyslip_verify(image_bytes: bytes):
    """เรียก EasySlip -> คืน dict (verified, amount, receiver_acc, receiver_bank, ref, date) หรือ None"""
    if not EASYSLIP_TOKEN:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://developer.easyslip.com/api/v1/verify",
                             headers={"Authorization": f"Bearer {EASYSLIP_TOKEN}"},
                             json={"image": b64})
        j = r.json()
        if j.get("status") != 200:
            return {"verified": False, "error": j.get("message") or j.get("status"), "raw": j}
        d = j.get("data") or {}
        amt = d.get("amount")
        amount = amt.get("amount") if isinstance(amt, dict) else amt
        recv = d.get("receiver") or {}
        racc = (recv.get("account") or {})
        return {
            "verified": True,
            "amount": amount,
            "receiver_acc": racc.get("bank", {}).get("account") or racc.get("proxy", {}).get("account") or racc.get("value"),
            "receiver_name": racc.get("name", {}).get("th") or racc.get("name", {}).get("en") if isinstance(racc.get("name"), dict) else racc.get("name"),
            "receiver_bank": (recv.get("bank") or {}).get("short") or (recv.get("bank") or {}).get("name"),
            "ref": d.get("transRef") or d.get("ref"),
            "date": d.get("date") or d.get("transDate"),
            "raw": j,
        }
    except Exception as e:
        print("easyslip error:", e)
        return None


async def _gemini_classify_slip(image_bytes: bytes, mime: str) -> dict | None:
    """ใช้ Claude (vision) เดาว่ารูปนี้เป็นสลิปโอนเงิน/หลักฐานชำระเงินไหม + อ่านข้อมูลคร่าว ๆ
    (ยอด/ธนาคาร/เลขบัญชี/ref/วันที่) — เป็นแค่ตัวช่วยกรอง+อ่านเบื้องต้น ไม่ใช่การยืนยันที่เชื่อถือได้
    100% (ไม่มี fraud-check เหมือน EasySlip) คืน None ถ้าปิดฟีเจอร์ (ไม่ตั้ง ANTHROPIC_API_KEY) หรือ error"""
    if not ANTHROPIC_API_KEY or not image_bytes:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        "ดูรูปนี้แล้วบอกว่าเป็น \"สลิปโอนเงิน/หลักฐานการชำระเงิน\" (สลิปธนาคาร, mobile banking, "
        "พร้อมเพย์ ใบเสร็จโอนเงิน ฯลฯ) หรือไม่ ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่นนอก JSON ห้ามมี markdown "
        "code fence ตามโครงสร้างนี้เป๊ะ ๆ:\n"
        '{"is_slip": true หรือ false, "confidence": ตัวเลข 0 ถึง 1 (ความมั่นใจในคำตอบ is_slip), '
        '"amount": จำนวนเงิน (ตัวเลขล้วน) หรือ null, "bank": ชื่อธนาคาร หรือ null, '
        '"receiver_account": เลขบัญชี/พร้อมเพย์ปลายทางที่อ่านได้ หรือ null, '
        '"ref": เลขที่อ้างอิงธุรกรรม หรือ null, "date": วันที่ทำรายการ รูปแบบ YYYY-MM-DD หรือ null}\n'
        "ถ้าไม่ใช่สลิป (เช่น รูปคน, สติกเกอร์, สกรีนช็อตแชท, การ์ตูน) ให้ is_slip=false และ field อื่น "
        "เป็น null ทั้งหมด"
    )
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=8)
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=400, temperature=0.1,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": mime or "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(text)
        return {
            "is_slip": bool(data.get("is_slip")),
            "confidence": float(data.get("confidence") or 0),
            "amount": data.get("amount"),
            "bank": data.get("bank"),
            "receiver_account": data.get("receiver_account"),
            "ref": data.get("ref"),
            "date": data.get("date"),
        }
    except Exception as e:
        print("claude slip classify error:", e)
        return None


async def _handle_slips(img_events: list):
    notify_all = bool(await _get_setting("slip_notify_all_images", True))

    for e in img_events:
        uid = e.get("source", {}).get("userId")
        mid = e.get("message", {}).get("id")
        mtype = e.get("message", {}).get("type")
        if not uid or not mid:
            continue

        content, ctype = await line.get_message_content(mid)
        media_url = None
        if content:
            ext = {"image/png": "png", "image/jpeg": "jpg"}.get(ctype, "jpg" if mtype == "image" else "bin")
            path = f"slips/{dt.date.today().isoformat()}/{mid}.{ext}"
            try:
                media_url = await supa.storage_upload("media", path, content, ctype or "image/jpeg")
                await supa.update("messages", {"media_url": media_url},
                                  {"line_user_id": f"eq.{uid}", "payload->>id": f"eq.{mid}"})
            except Exception as ex:
                print("slip upload error:", ex)

        # เป็นสลิปไหม — ดูจาก context: เคยคุยเรื่องชำระเงินใน 24 ชม.ล่าสุด (heuristic แบบเดิม)
        pay_ctx = False
        blob = ""
        try:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
            recent = await supa.select("messages", params={
                "select": "text", "line_user_id": f"eq.{uid}",
                "created_at": f"gte.{since}", "order": "created_at.desc", "limit": "15"})
            blob = " ".join((r.get("text") or "") for r in recent)
            pay_ctx = any(w in blob for w in ("ชำระ", "โอน", "สลิป", "สมัคร", "ค่าติว", "บัญชี", "หลักสูตร"))
        except Exception:
            blob = ""

        # ---- gate: รูปนี้เป็น "สลิป" ไหม ----
        # คุยเรื่องเงินใน 24 ชม. (pay_ctx) เชื่อได้เลย; ถ้าไม่มี -> ถาม Gemini (timeout 8s)
        gclass = None
        is_slip = pay_ctx
        if content and not pay_ctx:
            gclass = await _gemini_classify_slip(content, ctype)
            is_slip = bool(gclass and gclass.get("is_slip") and (gclass.get("confidence") or 0) >= 0.55)

        if not (is_slip or notify_all):
            continue  # ไม่ใช่สลิป + ไม่ได้ตั้งให้แจ้งทุกรูป -> ข้าม (ไม่สร้าง row)

        # ---- เดาหลักสูตรจากบทสนทนา ----
        exp_course = None
        for c in ("FC", "IC", "PC", "AC"):
            if f"หลักสูตร {c}" in blob or f"course={c}" in blob or f"{c}70" in blob:
                exp_course = c
                break

        # ============ ทำสิ่งที่สำคัญต่อเวลาก่อน (ก่อนอ่านสลิปที่ช้า) ============
        # 1) สร้าง slip row ทันที (status=new) จะได้ไม่หายถ้า handler ถูกตัดกลางคัน
        slip_id = None
        try:
            slip_row = await supa.insert("slips", {
                "line_user_id": uid, "message_id": mid, "media_url": media_url,
                "status": "new", "expected_course": exp_course})
            slip_id = (slip_row[0]["id"] if slip_row else None)
        except Exception as ex:
            print("slip pre-insert error:", ex)

        # 2) สลับ rich menu ทันที — เฉพาะเมื่อมั่นใจว่าเป็นสลิป (notify_all อย่างเดียวไม่สลับ)
        if is_slip and SLIP_SUCCESS_RICHMENU_ID:
            try:
                old_map = await _fetch_current_menu_map([uid])
                ok_link, _code = await line.user_richmenu_link(uid, SLIP_SUCCESS_RICHMENU_ID)
                if ok_link:
                    ts_link = NOW()
                    new_row = {"line_user_id": uid, "current_rich_menu_id": SLIP_SUCCESS_RICHMENU_ID,
                               "rich_menu_status": "assigned", "rich_menu_checked_at": ts_link,
                               "updated_at": ts_link}
                    await supa.upsert("line_users", [new_row], on_conflict="line_user_id")
                    await _log_richmenu_diffs(old_map, [new_row], "slip", "system")
            except Exception as ex:
                print("slip richmenu link error:", ex)

        # ============ ตอนนี้ค่อยอ่านสลิป (ช้า) แล้ว update row ============
        ocr = await _easyslip_verify(content) if content else None
        easyslip_err = ocr.get("error") if ocr and not ocr.get("verified") else None
        if not ocr or easyslip_err:
            # EasySlip อ่านไม่ได้ หรือ error (เช่น quota_exceeded/rate_limit) -> ใช้ Gemini อ่านแทน
            # (ถ้ายังไม่ได้เรียกใน gate) กันไม่ให้ตกไป "ตรวจสลิปไม่ผ่าน" เฉยๆ ทั้งที่ยังพอเดาข้อมูลได้
            if gclass is None and content:
                gclass = await _gemini_classify_slip(content, ctype)
            if gclass and gclass.get("is_slip"):
                ocr = {"verified": False, "amount": gclass.get("amount"),
                       "receiver_acc": gclass.get("receiver_account"), "receiver_bank": gclass.get("bank"),
                       "ref": gclass.get("ref"), "date": gclass.get("date"),
                       "source": "gemini", "confidence": gclass.get("confidence"),
                       "easyslip_error": easyslip_err}

        status, matched, auto_note, dup_ref = "new", None, None, None
        amount = ocr.get("amount") if ocr else None
        ref = ocr.get("ref") if ocr else None

        # ---- ตรวจ ref ซ้ำ (ทุก source ไม่ใช่แค่ EasySlip) ----
        if ref:
            exist = await supa.select("slips", params={
                "ref": f"eq.{ref}", "id": f"neq.{slip_id or 0}",
                "select": "id", "limit": "1"})
            if exist:
                status, dup_ref = "rejected", ref
                auto_note = "สลิปนี้เคยส่งมาแล้ว (ref ซ้ำ)"

        if status != "rejected" and ocr and ocr.get("verified"):
            accounts = await supa.select("payment_accounts", params={"select": "*", "active": "eq.true"})
            racc = _digits(ocr.get("receiver_acc"))
            acc = next((a for a in accounts if racc and (_digits(a["account_no"])[-4:] == racc[-4:])), None)
            if not acc:
                status, auto_note = "review", f"บัญชีปลายทางไม่ตรงรายการ ({ocr.get('receiver_acc')})"
            else:
                def _near(target):
                    return target not in (None, "") and abs(float(amount or 0) - float(target)) < 1
                matched = _near(acc.get("price")) or _near(acc.get("full_price"))
                exp_course = exp_course or acc["course"]
                if matched:
                    status, auto_note = "verified", f"✓ {acc['course']} · {amount} บาท · {acc['bank']}"
                else:
                    status, auto_note = "review", f"ยอดไม่ตรง: โอน {amount} / ราคา {acc['price']} ({acc['course']})"
        elif status != "rejected" and ocr and ocr.get("source") == "gemini":
            conf_pct = round((ocr.get("confidence") or 0) * 100)
            extra = f" (EasySlip: {ocr.get('easyslip_error')})" if ocr.get("easyslip_error") else ""
            status, auto_note = "review", f"🤖 Gemini เดาว่าอาจเป็นสลิป (มั่นใจ {conf_pct}%){extra} — ยังไม่ตรวจยอด/ธนาคารจริง รบกวนแอดมินเช็คเอง"
        elif status != "rejected" and ocr and not ocr.get("verified"):
            status, auto_note = "review", f"ตรวจสลิปไม่ผ่าน: {ocr.get('error')}"
        elif status != "rejected" and EASYSLIP_TOKEN:
            status, auto_note = "review", "อ่านสลิปไม่ได้"

        # 3) update slip row ด้วยผลที่อ่านได้
        patch = {"ocr": ocr, "amount": amount, "ref": None if dup_ref else ref,
                 "bank": ocr.get("receiver_bank") if ocr else None,
                 "slip_date": str(ocr.get("date")) if ocr and ocr.get("date") else None,
                 "status": status, "matched": matched, "auto_note": auto_note,
                 "dup_ref": dup_ref, "expected_course": exp_course}
        try:
            if slip_id:
                await supa.update("slips", patch, {"id": f"eq.{slip_id}"})
            else:
                await supa.insert("slips", {**patch, "line_user_id": uid, "message_id": mid, "media_url": media_url})
        except Exception as ex:
            print("slip update error:", ex)

        # ตอบ user ตามผลตรวจอัตโนมัติ
        if status == "verified":
            await line.push(uid, [{"type": "text", "text": f"✅ ตรวจสอบสลิปเรียบร้อยแล้ว\nยอด {amount} บาท · หลักสูตร {exp_course}\nขอบคุณครับ 🙏"}])
            try:
                await _enroll_automations("slip_verified", [uid], {"course": exp_course})
                await _mark_registration_paid(uid, exp_course)
                await _send_receipt(uid, {"id": slip_id, "amount": amount, "course": exp_course,
                                          "bank": (ocr or {}).get("receiver_bank"), "ref": ref,
                                          "date": (ocr or {}).get("date")})
            except Exception as ex:
                print("slip_verified post error:", ex)
        elif status == "rejected":
            await line.push(uid, [{"type": "text", "text": "สลิปนี้เคยส่งเข้ามาแล้วครับ หากต้องการสอบถามเพิ่มเติมพิมพ์ 'ติดต่อแอดมิน'"}])

        # แจ้งแอดมิน
        prof = await line.get_profile(uid)
        name = (prof or {}).get("displayName") or uid[:10]
        emoji = {"verified": "✅", "rejected": "⛔", "review": "⚠️"}.get(status, "🧾")
        detail = f"จาก: {name}\n" + (auto_note + "\n" if auto_note else "") + f"เปิดดู: {APP_URL}/slips"
        await alert_admin(f"{emoji} สลิป: {status}", detail, throttle_key=f"slip_{mid}")


async def _send_receipt(uid: str, s: dict):
    """ส่งใบเสร็จให้ลูกค้าหลังสลิป verified (ปิดได้ด้วย setting send_receipt=false)"""
    try:
        if await _get_setting("send_receipt", True) is False:
            return
    except Exception:
        pass
    acc = None
    try:
        c = str(s.get("course") or "")[:2].upper()
        accts = await supa.select("payment_accounts", params={"select": "*", "active": "eq.true"})
        acc = next((a for a in accts if str(a.get("course") or "").upper().startswith(c)), None)
    except Exception:
        pass
    org = ""
    try:
        org = str(await _get_setting("receipt_org", "") or "")
    except Exception:
        pass
    lines = ["📄 ใบเสร็จรับเงิน" + (f" — {org}" if org else ""),
             f"เลขที่: R{s.get('id') or '-'}",
             f"วันที่: {str(s.get('date') or dt.date.today().isoformat())[:10]}",
             f"หลักสูตร: {s.get('course') or '-'}",
             f"จำนวนเงิน: {s.get('amount') or '-'} บาท"]
    if acc:
        lines.append(f"ชำระเข้า: {acc.get('bank')} {acc.get('account_no')} ({acc.get('account_name')})")
    if s.get("ref"):
        lines.append(f"อ้างอิง: {s['ref']}")
    lines.append("\nขอบคุณที่ชำระเงินครับ 🙏 เก็บข้อความนี้ไว้เป็นหลักฐาน")
    try:
        await line.push(uid, [{"type": "text", "text": "\n".join(lines)}])
    except Exception as e:
        print("receipt push error:", e)


async def _mark_registration_paid(uid: str, course_hint: str | None = None):
    """สลิป verified -> mark registration ของ user คนนั้นเป็นจ่ายแล้ว (match คอร์ส FC->FC70*)"""
    if not uid:
        return
    try:
        params = {"line_user_id": f"eq.{uid}", "paid": "eq.false"}
        if course_hint:
            params["course"] = f"like.{course_hint.upper()}*"
        await supa.update("registrations", {"paid": True, "updated_at": NOW()}, params)
    except Exception as e:
        print("mark registration paid error:", e)


async def _enroll_automations(trigger: str, uids: list[str], ctx: dict | None = None):
    ctx = ctx or {}
    autos = await supa.select("automations", params={
        "select": "id,steps,trigger_config", "enabled": "eq.true", "trigger": f"eq.{trigger}"})
    if not autos or not uids:
        return
    rows = []
    for a in autos:
        cfg = a.get("trigger_config") or {}
        # กรองตาม trigger_config
        if trigger == "slip_verified" and cfg.get("course"):
            if str(cfg["course"]).upper() != str(ctx.get("course") or "").upper():
                continue
        if trigger == "tag_added" and cfg.get("tag"):
            if cfg["tag"] not in (ctx.get("tags") or []):
                continue
        steps = a.get("steps") or []
        if not steps:
            continue
        delay = int(steps[0].get("delayHours", 0)) * 3600 + int(steps[0].get("delayMinutes", 0)) * 60
        run_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)).isoformat()
        for uid in uids:
            rows.append({"automation_id": a["id"], "line_user_id": uid, "step_idx": 0,
                         "run_at": run_at, "status": "pending"})
    if rows:
        # on_conflict = ไม่ลง run ซ้ำถ้าคน ๆ นั้นเคยเข้า step 0 ของ automation นี้แล้ว
        try:
            await supa.upsert("automation_runs", rows, on_conflict="automation_id,line_user_id,step_idx")
        except Exception:
            for r in rows:
                try:
                    await supa.insert("automation_runs", r)
                except Exception:
                    pass


def _match(rule: dict, text: str) -> bool:
    t = (text or "").lower().strip()
    kws = [k.lower().strip() for k in (rule.get("keywords") or []) if k.strip()]
    mt = rule.get("match_type", "contains")
    if mt in ("any", "welcome"):
        return mt == "any"  # welcome จับที่ event follow แยกต่างหาก
    if not kws:
        return False
    if mt == "exact":
        return t in kws
    if mt == "prefix":
        return any(t.startswith(k) for k in kws)
    return any(k in t for k in kws)  # contains / postback


# trigger -> ประเภท event/message ที่ทำให้กฎทำงาน
TEXT_TRIGGERS = ("text", "fallback")
MSG_TYPE_TRIGGERS = ("sticker", "image", "video", "audio", "file", "location")

_PLACEHOLDERS = ("{name}", "{displayName}", "{{name}}", "{{displayName}}", "{ชื่อ}")


def _has_placeholder(messages: list) -> bool:
    return any(isinstance(m, dict) and m.get("type") == "text"
              and any(p in (m.get("text") or "") for p in _PLACEHOLDERS)
              for m in messages)


def _personalize(messages: list, name: str) -> list:
    name = name or "เพื่อน"
    out = []
    for m in messages:
        m2 = dict(m) if isinstance(m, dict) else m
        if isinstance(m2, dict) and m2.get("type") == "text" and m2.get("text"):
            txt = m2["text"]
            for p in _PLACEHOLDERS:
                txt = txt.replace(p, name)
            m2["text"] = txt
        out.append(m2)
    return out


async def _reply_rule(reply_token: str, rule: dict, name: str | None = None):
    msgs = rule["messages"][:5]
    if name is not None and _has_placeholder(msgs):
        msgs = _personalize(msgs, name)
    code, _ = await line.reply(reply_token, msgs)
    if code == 200:
        await supa.update("auto_replies",
                          {"hits": (rule.get("hits") or 0) + 1, "last_hit_at": NOW()},
                          {"id": f"eq.{rule['id']}"})
    return code == 200


def _pick_rule(event: dict, rules: list):
    """เลือกกฎแรกที่ตรงกับ event (rules เรียง priority.desc แล้ว)"""
    et = event["type"]
    if et == "follow":
        return next((r for r in rules if r["trigger"] == "follow"), None)
    if et == "postback":
        data = event.get("postback", {}).get("data", "")
        return next((r for r in rules if r["trigger"] == "postback" and _match(r, data)), None)
    if et == "beacon":
        return next((r for r in rules if r["trigger"] == "beacon"), None)
    if et == "message":
        mtype = event.get("message", {}).get("type")
        if mtype == "text":
            text = event["message"]["text"]
            r = next((r for r in rules if r["trigger"] == "text" and _match(r, text)), None)
            if r:
                return r
            # ไม่มีคีย์เวิร์ดไหนตรง -> fallback
            return next((r for r in rules if r["trigger"] == "fallback"), None)
        if mtype in MSG_TYPE_TRIGGERS:
            return next((r for r in rules if r["trigger"] == mtype), None)
    return None


_gemini_ctx_cache: dict = {"text": None, "exp": 0.0}


async def _gemini_context() -> str:
    """รวมข้อมูลอ้างอิงให้ Gemini: ข้อความธุรกิจที่ตั้งเอง + ราคาคอร์ส/บัญชี + FAQ จากกฎ auto-reply
    cache 5 นาที"""
    if _gemini_ctx_cache["text"] is not None and time.time() < _gemini_ctx_cache["exp"]:
        return _gemini_ctx_cache["text"]
    parts: list[str] = []
    try:
        bi = await _get_setting("gemini_context", "")
        if bi and str(bi).strip():
            parts.append(str(bi).strip())
    except Exception:
        pass
    try:
        accts = await supa.select("payment_accounts", params={
            "select": "course,bank,account_no,account_name,price,full_price", "active": "eq.true"})
        if accts:
            lines = ["ราคาคอร์สและบัญชีโอนเงิน:"]
            for a in accts:
                lines.append(f"- คอร์ส {a['course']}: ราคา {a.get('price')} บาท (เต็ม {a.get('full_price')}) "
                             f"โอนเข้า {a.get('bank')} เลขบัญชี {a.get('account_no')} ชื่อ {a.get('account_name')}")
            parts.append("\n".join(lines))
    except Exception:
        pass
    text = "\n\n".join(parts)[:4000]
    _gemini_ctx_cache["text"] = text
    _gemini_ctx_cache["exp"] = time.time() + 300
    return text


_gemini_kb_cache: dict = {"items": None, "exp": 0.0}


async def _gemini_kb_items() -> list:
    """คลังความรู้ + FAQ จาก auto_replies — [{title, body, kw:set}] cache 5 นาที"""
    if _gemini_kb_cache["items"] is not None and time.time() < _gemini_kb_cache["exp"]:
        return _gemini_kb_cache["items"]
    items: list = []
    try:
        for a in await supa.select("kb_articles", params={
                "select": "title,body,keywords", "enabled": "eq.true", "limit": "300"}):
            items.append({"title": a["title"], "body": a["body"],
                          "kw": set((a.get("keywords") or []) + a["title"].lower().split())})
    except Exception:
        pass
    try:
        for r in await supa.select("auto_replies", params={
                "select": "keywords,messages", "enabled": "eq.true",
                "trigger": "in.(text,fallback)", "order": "priority.desc", "limit": "80"}):
            kws = [k for k in (r.get("keywords") or []) if k]
            ans = " ".join(m.get("text", "") for m in (r.get("messages") or [])
                           if isinstance(m, dict) and m.get("type") == "text" and m.get("text"))
            if kws and ans:
                items.append({"title": ", ".join(kws), "body": ans[:400],
                              "kw": set(k.lower() for k in kws)})
    except Exception:
        pass
    _gemini_kb_cache["items"] = items
    _gemini_kb_cache["exp"] = time.time() + 300
    return items


def _tok(s: str) -> set:
    return set(w for w in re.findall(r"[ก-๙a-zA-Z0-9]{2,}", (s or "").lower()))


# คำพ้องความหมายที่ลูกค้ามักใช้ต่างจากคำในคลังความรู้ — ขยายฝั่งคำถามก่อนให้คะแนน
# ช่วยให้ "เท่าไหร่/กี่บาท" เจอบทความที่เขียนว่า "ราคา" ได้ แม้คำไม่ตรงเป๊ะ
_SYN_GROUPS = [
    {"ราคา", "เท่าไหร่", "เท่าไร", "ค่าเรียน", "ค่าลงทะเบียน", "กี่บาท", "ค่าใช้จ่าย", "แพง", "ถูก"},
    {"โอน", "จ่าย", "ชำระ", "จ่ายเงิน", "ชำระเงิน", "โอนเงิน", "โอนแล้ว"},
    {"เรียน", "คอร์ส", "หลักสูตร", "คลาส", "รุ่น"},
    {"สมัคร", "ลงทะเบียน", "ลงชื่อ", "สมัครเรียน", "สมัครติว"},
    {"ผ่อน", "แบ่งจ่าย", "แบ่งชำระ", "ผ่อนชำระ", "ผ่อนได้ไหม"},
    {"ยกเลิก", "เลิก", "ขอคืนเงิน", "คืนเงิน", "ไม่เรียนแล้ว", "ไม่เอาแล้ว"},
    {"ซูม", "zoom", "ลิงก์เรียน", "ลิงค์เรียน", "ห้องเรียน", "ห้องซูม"},
    {"เอกสาร", "pdf", "ไฟล์", "สรุป", "ชีท", "เนื้อหา"},
    {"เริ่มเรียน", "เปิดเรียน", "วันเริ่ม", "วันที่เริ่ม", "ตารางเรียน", "ตาราง", "เมื่อไหร่", "วันไหน"},
    {"สลิป", "หลักฐานโอน", "ใบเสร็จ", "receipt", "สลิปโอน"},
    {"ที่อยู่", "สถานที่", "ที่ไหน", "location", "เรียนที่ไหน"},
    {"ผล", "ประกาศผล", "ผลสอบ", "ติดไหม", "ผ่านไหม"},
]
_SYN_MAP: dict = {}
for _grp in _SYN_GROUPS:
    for _w in _grp:
        _SYN_MAP.setdefault(_w, set()).update(_grp)


def _expand_syn(tokens: set) -> set:
    out = set(tokens)
    for t in tokens:
        out |= _SYN_MAP.get(t, set())
    return out


async def _gemini_retrieve(question: str, k: int = 6) -> str:
    """ดึงบทความ KB/FAQ ที่เกี่ยวกับคำถามนี้มากสุด k อัน (ขยายคำพ้องความหมายฝั่งคำถามก่อนให้คะแนน)"""
    items = await _gemini_kb_items()
    if not items:
        return ""
    qt = _expand_syn(_tok(question))
    scored = []
    for it in items:
        body_t = _tok(it["body"])
        score = len(qt & it["kw"]) * 3 + len(qt & _tok(it["title"])) * 2 + len(qt & body_t)
        if score:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    if not top:
        return ""
    return "ข้อมูลที่เกี่ยวข้อง (ใช้ตอบ):\n" + "\n\n".join(
        f"[{it['title']}]\n{it['body'][:500]}" for _s, it in top)


# tool schema แบบ Anthropic (name/description/input_schema) — ใช้กับ Claude ผ่าน _gemini_answer
_GEMINI_TOOLS = [
    {
        "name": "get_my_account",
        "description": "ดูข้อมูลการลงทะเบียนเรียนและการชำระเงินของผู้ใช้ที่กำลังคุยอยู่ "
                       "(หลักสูตรที่ลงทะเบียน, จ่ายแล้ว/ยัง, สถานะสลิปล่าสุด)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_course_details",
        "description": "ดูรายละเอียดหลักสูตร: ราคา ราคาเต็ม ธนาคาร เลขบัญชี ชื่อบัญชีสำหรับโอนเงิน",
        "input_schema": {"type": "object", "properties": {
            "course": {"type": "string", "description": "รหัสหลักสูตร เช่น FC IC PC AC"}}, "required": ["course"]},
    },
    {
        "name": "get_class_info",
        "description": "ดูข้อมูลคลาส/รุ่นของหลักสูตร: วันเริ่มเรียน ตารางเรียน ลิงก์ Zoom ลิงก์เอกสาร",
        "input_schema": {"type": "object", "properties": {
            "course": {"type": "string", "description": "รหัสหลักสูตร เช่น FC IC PC"}}, "required": ["course"]},
    },
    {
        "name": "search_knowledge",
        "description": "ค้นหาข้อมูลเพิ่มเติมในคลังความรู้ของเพจ ใช้เมื่อยังตอบคำถามไม่ได้จากข้อมูลที่มี",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "คำค้น"}}, "required": ["query"]},
    },
    {
        "name": "note_interest",
        "description": "บันทึกว่าผู้ใช้สนใจหลักสูตรนี้ (ยังไม่ได้ลงทะเบียน) เรียกเมื่อผู้ใช้แสดงความสนใจอยากเรียน/สมัคร",
        "input_schema": {"type": "object", "properties": {
            "course": {"type": "string", "description": "รหัสหลักสูตรที่สนใจ"}}, "required": ["course"]},
    },
    {
        "name": "escalate_to_admin",
        "description": "เรียกแอดมินมาช่วย — ใช้เมื่อผู้ใช้ต้องการคุยกับคน, ร้องเรียน, หรือคำถามที่ต้องให้คนตัดสินใจ",
        "input_schema": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "สรุปสั้น ๆ ว่าผู้ใช้ต้องการอะไร"}}, "required": ["reason"]},
    },
]

_gemini_escalations: dict = {}   # uid -> reason (อ่านหลัง reply เพื่อ bump unread/tag)

# คำที่บ่งชี้ว่าควรส่งต่อคนโดยไม่พึ่งดุลพินิจโมเดลอย่างเดียว (safety net ธุรกิจ)
_URGENT_KW = ("ร้องเรียน", "ไม่พอใจ", "โกง", "หลอกลวง", "ฟ้อง", "แจ้งความ", "แจ้งจับ", "ทนาย",
              "ขอเงินคืน", "คืนเงิน", "เร่งด่วน", "ด่วนมาก", "ผิดพลาดร้ายแรง", "เสียหาย")


def _is_urgent(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _URGENT_KW)


async def _gemini_tool_exec(name: str, args: dict, uid: str) -> dict:
    try:
        if name == "get_my_account":
            regs = await supa.select("registrations", params={
                "line_user_id": f"eq.{uid}", "select": "course,course_raw,paid,approved"})
            slips = await supa.select("slips", params={
                "line_user_id": f"eq.{uid}", "select": "status,amount,expected_course,auto_note,created_at",
                "order": "created_at.desc", "limit": "3"})
            if not regs and not slips:
                return {"note": "ยังไม่พบข้อมูลการลงทะเบียนของผู้ใช้คนนี้ในระบบ"}
            return {
                "registrations": [{"course": r.get("course_raw") or r.get("course"),
                                   "paid": bool(r.get("paid")),
                                   "approved": r.get("approved")} for r in regs],
                "recent_slips": [{"status": s.get("status"), "amount": s.get("amount"),
                                  "course": s.get("expected_course"), "note": s.get("auto_note"),
                                  "date": (s.get("created_at") or "")[:16]} for s in slips],
            }
        if name == "get_course_details":
            c = re.sub(r"[^A-Za-z]", "", str(args.get("course") or "")).upper()[:2]
            accts = await supa.select("payment_accounts", params={"select": "*", "active": "eq.true"})
            m = next((a for a in accts if str(a.get("course") or "").upper().startswith(c)), None)
            if not m:
                return {"error": f"ไม่พบหลักสูตร {args.get('course')}"}
            return {"course": m["course"], "price": m.get("price"), "full_price": m.get("full_price"),
                    "bank": m.get("bank"), "account_no": m.get("account_no"), "account_name": m.get("account_name")}
        if name == "get_class_info":
            c = re.sub(r"[^A-Za-z]", "", str(args.get("course") or "")).upper()[:2]
            cls = await supa.select("classes", params={
                "select": "name,course,cohort,start_date,schedule,zoom_url,materials_url,status,capacity",
                "course": f"like.{c}*", "status": "in.(open,running)",
                "order": "start_date.asc", "limit": "5"})
            if not cls:
                return {"note": f"ยังไม่มีคลาส {args.get('course')} ที่เปิดอยู่ตอนนี้"}
            return {"classes": cls}
        if name == "search_knowledge":
            snip = await _gemini_retrieve(str(args.get("query") or ""), k=5)
            return {"result": snip or "ไม่พบข้อมูลที่เกี่ยวข้องในคลังความรู้"}
        if name == "note_interest":
            course = re.sub(r"[^A-Za-z0-9]", "", str(args.get("course") or "")).upper()
            try:
                u = await supa.select("line_users", params={
                    "select": "stage,tags", "line_user_id": f"eq.{uid}", "limit": "1"})
                cur = u[0] if u else {}
                patch = {"tags": sorted(set(cur.get("tags") or []) | {f"สนใจ-{course}" if course else "สนใจ"}),
                         "updated_at": NOW()}
                if not cur.get("stage") or cur.get("stage") == "lead":
                    patch["stage"] = "interested"
                    patch["stage_at"] = NOW()
                await supa.update("line_users", patch, {"line_user_id": f"eq.{uid}"})
            except Exception as e:
                print("note_interest err:", e)
            return {"ok": True}
        if name == "escalate_to_admin":
            _gemini_escalations[uid] = str(args.get("reason") or "ผู้ใช้ขอคุยกับแอดมิน")[:200]
            return {"ok": True, "message": "แจ้งแอดมินแล้ว จะรีบติดต่อกลับ"}
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"error": "unknown tool"}


async def _gemini_answer(question: str, temperature: float = 0.7, context: str = "",
                         history: list | None = None, uid: str | None = None,
                         user_facts: str = "") -> str | None:
    """ตอบด้วย Claude (Anthropic) + ประวัติการคุย + เครื่องมือดูข้อมูลบัญชี/หลักสูตร — คืน None ถ้า error/ว่าง"""
    if not ANTHROPIC_API_KEY or not question.strip():
        return None
    sys_text = (
        "คุณเป็นผู้ช่วยของเพจติวสอบ ตอบสมาชิกทางไลน์ เป็นภาษาไทย เป็นกันเอง กระชับ ไม่เกิน 4-5 ประโยค "
        "ห้ามขึ้นต้นว่า 'สวัสดีครับ/ค่ะ' ทุกครั้ง (คุยต่อเนื่องอยู่) "
        "ห้ามใช้ markdown เช่น **ตัวหนา** #หัวข้อ หรือ bullet ที่ขึ้นต้นด้วย * เพราะไลน์แสดงตัวอักษรดิบ ให้เขียนเป็นประโยคปกติหรือขึ้นบรรทัดใหม่แทน "
        "ตอบตามข้อมูลอ้างอิง/ผลลัพธ์จากเครื่องมือเท่านั้น ห้ามเดาราคา วันที่ หรือสถานะที่ไม่มีในข้อมูลที่ได้รับ "
        "เมื่อผู้ใช้ถามเรื่องการลงทะเบียน/การจ่ายเงิน/สลิปของตัวเอง ให้เรียก get_my_account ก่อนตอบเสมอ "
        "ถ้าเครื่องมือบอกว่ายังไม่พบข้อมูล ให้ขอชื่อ-เบอร์โทรที่ใช้ตอนสมัครเพื่อส่งต่อแอดมินตรวจสอบ ห้ามเดาว่าจ่ายแล้วหรือยัง "
        "เมื่อถามราคา/บัญชีโอนเงินของหลักสูตร ให้เรียก get_course_details "
        "เมื่อถามวันเริ่มเรียน/ตารางเรียน/ลิงก์ซูมของคลาส ให้เรียก get_class_info "
        "ถ้าผู้ใช้ขอคุยกับคน/ร้องเรียน/แสดงความไม่พอใจ/เรื่องเร่งด่วน/เรื่องที่ต้องให้คนตัดสิน ให้เรียก escalate_to_admin ทันทีโดยไม่ต้องพยายามแก้ปัญหาเอง "
        "ถ้าไม่มีข้อมูลและเครื่องมือช่วยไม่ได้ ให้บอกตรง ๆ ว่าไม่แน่ใจ แนะนำพิมพ์ 'ติดต่อแอดมิน' ห้ามแต่งคำตอบขึ้นเอง")
    if user_facts:
        sys_text += f"\n\nข้อมูลผู้ใช้ที่กำลังคุย: {user_facts}"
    if context:
        sys_text += "\n\n=== ข้อมูลอ้างอิง ===\n" + context

    messages: list = []
    for m in (history or [])[-8:]:
        role = "assistant" if m["role"] in ("model", "assistant") else "user"
        messages.append({"role": role, "content": m["text"][:800]})
    messages.append({"role": "user", "content": question[:2000]})

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=25)
    kwargs = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "temperature": max(0.0, min(1.0, temperature if temperature is not None else 0.7)),
        "system": sys_text,
        "messages": messages,
    }
    if uid:
        kwargs["tools"] = _GEMINI_TOOLS

    def _text_of(resp):
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    try:
        last_text = ""
        for _round in range(5):
            resp = await client.messages.create(**kwargs)
            txt = _text_of(resp)
            if txt:
                last_text = txt
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                if txt:
                    return txt
                break  # จบแต่ไม่มีข้อความ -> ลองอีกรอบไม่ใช้ tool
            kwargs["messages"].append({"role": "assistant", "content": resp.content})
            tool_results = []
            for tu in calls:
                res = await _gemini_tool_exec(tu.name, tu.input or {}, uid or "")
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": json.dumps(res, ensure_ascii=False)})
            kwargs["messages"].append({"role": "user", "content": tool_results})

        if last_text:
            return last_text
        # รอบสุดท้าย: บังคับให้ตอบเป็นข้อความ ไม่มี tool
        kwargs.pop("tools", None)
        kwargs["messages"].append({"role": "user", "content": "สรุปคำตอบเป็นข้อความสั้น ๆ ให้ลูกค้าเลย"})
        resp = await client.messages.create(**kwargs)
        return _text_of(resp) or None
    except anthropic.APIStatusError as e:
        print("claude http error:", e.status_code, str(e.message)[:400])
        return None
    except Exception as e:
        print("claude error:", repr(e))
        return None


async def _gemini_enabled_menu_ids() -> set:
    """rich_menu_id ทั้งหมดที่เปิด Gemini/AI (gemini_enabled=true หรือชื่อตรง GEMINI_TRIGGER_MENU_NAME)"""
    if not ANTHROPIC_API_KEY:
        return set()
    ids: set = set()
    try:
        for m in await supa.select("rich_menus", params={
                "select": "rich_menu_id", "gemini_enabled": "eq.true"}):
            ids.add(m["rich_menu_id"])
    except Exception as e:
        print("gemini menu ids error:", e)
    if GEMINI_TRIGGER_MENU_NAME:
        try:
            for m in await supa.select("rich_menus", params={
                    "select": "rich_menu_id", "name": f"eq.{GEMINI_TRIGGER_MENU_NAME}"}):
                ids.add(m["rich_menu_id"])
        except Exception:
            pass
    return ids


async def _handle_gemini_replies(events: list) -> set:
    """ตอบข้อความที่ไม่ตรงกฎไหน ด้วย Claude (Anthropic) — เรียกหลัง _handle_auto_replies แล้ว
    (events ถูกกรองมาแล้วว่าเป็น text ของ user บนเมนู AI ที่ยังไม่มีใครตอบ)"""
    if not ANTHROPIC_API_KEY:
        return set()
    text_events = [e for e in events if e.get("type") == "message"
                   and e.get("message", {}).get("type") == "text"
                   and e.get("replyToken") and e.get("source", {}).get("userId")]
    if not text_events:
        return set()

    # menu_temp: {rich_menu_id: temperature} เฉพาะเมนูที่เปิด gemini_enabled ไว้
    menu_temp: dict[str, float] = {}
    try:
        menus = await supa.select("rich_menus", params={
            "select": "rich_menu_id,gemini_temperature", "gemini_enabled": "eq.true"})
        for m in menus:
            try:
                menu_temp[m["rich_menu_id"]] = float(m.get("gemini_temperature") or 0.7)
            except (TypeError, ValueError):
                menu_temp[m["rich_menu_id"]] = 0.7
    except Exception as e:
        print("gemini: read rich_menus error:", e)

    # backward-compat: เมนูที่ชื่อตรง GEMINI_TRIGGER_MENU_NAME (ตั้งค่าเก่าแบบ env var) ก็ยังนับด้วย
    if GEMINI_TRIGGER_MENU_NAME:
        try:
            legacy = await supa.select("rich_menus", params={
                "select": "rich_menu_id", "name": f"eq.{GEMINI_TRIGGER_MENU_NAME}"})
            for m in legacy:
                menu_temp.setdefault(m["rich_menu_id"], 0.7)
        except Exception:
            pass

    if not menu_temp:
        return set()

    ctx = await _gemini_context()
    _gemini_escalations.clear()
    handled: set = set()
    out_msgs = []
    for e in text_events:
        uid = e["source"]["userId"]
        if uid in handled:
            continue
        if not _rate_ok(f"gemini:{uid}", limit=6, window=60):  # กันสแปม/ต้นทุนบานปลาย
            continue
        try:
            rid = await line.user_richmenu_get(uid)
        except Exception:
            rid = None
        if rid not in menu_temp:
            continue
        question = e["message"].get("text") or ""

        # ประวัติการคุย 8 ข้อความล่าสุด (in=user, out=model) + ข้อมูลผู้ใช้
        history, user_facts = [], ""
        try:
            recent = await supa.select("messages", params={
                "select": "direction,by,text,msg_type", "line_user_id": f"eq.{uid}",
                "order": "created_at.desc", "limit": "9"})
            for m in reversed(recent):
                txt = (m.get("text") or "").strip()
                if not txt or m.get("msg_type") not in (None, "text"):
                    continue
                role = "user" if m.get("direction") == "in" else "model"
                if role == "model" and m.get("by") == "gemini" and txt == "":
                    continue
                history.append({"role": role, "text": txt})
            history = history[:-1] if history and history[-1]["role"] == "user" and history[-1]["text"] == question.strip() else history
            prof = await line.get_profile(uid)
            regs = await supa.select("registrations", params={
                "line_user_id": f"eq.{uid}", "select": "course_raw,course,paid", "limit": "10"})
            facts = []
            if prof and prof.get("displayName"):
                facts.append(f"ชื่อ LINE: {prof['displayName']}")
            if regs:
                facts.append("ลงทะเบียน: " + ", ".join(
                    f"{r.get('course_raw') or r.get('course')}({'จ่ายแล้ว' if r.get('paid') else 'ค้างจ่าย'})" for r in regs))
            user_facts = " · ".join(facts)
        except Exception as ex:
            print("gemini history/facts error:", ex)

        try:
            kb = await _gemini_retrieve(question)
        except Exception:
            kb = ""
        if not kb:
            # ไม่เจอบทความ/FAQ ที่เกี่ยวข้องเลย -> บันทึกไว้ให้แอดมินเห็นว่าควรเพิ่มความรู้เรื่องอะไร
            try:
                await supa.insert("operations", {"actor": "gemini", "action": "gemini.kb_gap",
                                                 "params": {"q": question[:200]}, "status": "ok"})
            except Exception:
                pass
        full_ctx = (ctx + ("\n\n" + kb if kb else "")).strip()
        answer = await _gemini_answer(question, menu_temp[rid], context=full_ctx,
                                      history=history, uid=uid, user_facts=user_facts)
        gemini_ok = bool(answer)
        if not answer:
            # AI ตอบไม่ได้ (quota/error) -> อย่าปล่อยเงียบ: ข้อความค้างไว้ + ให้แอดมินเห็น
            answer = "ได้รับข้อความแล้วครับ 🙏 เดี๋ยวแอดมินมาตอบให้นะครับ"
        if _is_urgent(question) and uid not in _gemini_escalations:
            # safety net: ข้อความมีลักษณะร้องเรียน/เร่งด่วน -> ส่งต่อแอดมินเสมอ ไม่ปล่อยให้โมเดลตัดสินใจเองอย่างเดียว
            _gemini_escalations[uid] = f"ข้อความอาจร้องเรียน/เร่งด่วน: {question[:100]}"
        code, _ = await line.reply(e["replyToken"], [{"type": "text", "text": answer[:4900]}])
        if code == 200:
            handled.add(uid)
            out_msgs.append({"line_user_id": uid, "direction": "out", "by": "gemini",
                             "msg_type": "text", "text": answer,
                             "payload": {"question": question, "model": ANTHROPIC_MODEL}})
            # Gemini เรียก escalate_to_admin หรือ AI ตอบไม่ได้ -> bump unread + tag ให้แอดมินเห็น
            if uid in _gemini_escalations or not gemini_ok:
                try:
                    ex = await supa.select("line_users", params={
                        "select": "unread,tags", "line_user_id": f"eq.{uid}", "limit": "1"})
                    cur = ex[0] if ex else {}
                    await supa.update("line_users", {
                        "unread": (cur.get("unread") or 0) + 1,
                        "tags": sorted(set(cur.get("tags") or []) | {"รอแอดมิน"}),
                        "updated_at": NOW(),
                    }, {"line_user_id": f"eq.{uid}"})
                    reason = _gemini_escalations.get(uid) or f"AI ตอบไม่ได้: {question[:80]}"
                    await alert_admin("🙋 ลูกค้ารอแอดมิน", f"{user_facts or uid[:12]}\n{reason}",
                                      throttle_key=f"esc_{uid}")
                except Exception as ex2:
                    print("escalation error:", ex2)
    if out_msgs:
        try:
            await supa.insert("messages", out_msgs)
        except Exception:
            pass
    return handled


async def _handle_auto_replies(events: list, gemini_uids: set = frozenset()) -> set:
    """ตอบกฎ auto-reply / postback / ต้อนรับ. คืน set ของ uid ที่ตอบไปแล้ว.
    user ใน gemini_uids: ข้ามกฎ fallback (ปล่อยให้ Gemini ตอบแทน) — กฎอื่น ๆ ทำงานปกติ"""
    repliable = [e for e in events if e.get("replyToken") and e.get("type") in
                 ("message", "follow", "postback", "beacon")]
    if not repliable:
        return set()
    rules = await supa.select("auto_replies", params={
        "select": "*", "enabled": "eq.true", "order": "priority.desc,id.asc", "limit": "300",
    })
    for r in rules:
        r.setdefault("trigger", "text")
    if not rules:
        return set()

    # หา user ที่ปิด auto-reply ไว้ (แอดมินกำลังคุยเอง)
    uids = {e.get("source", {}).get("userId") for e in repliable if e.get("source", {}).get("userId")}
    paused = set()
    if uids:
        rows = await supa.select("line_users", params={
            "select": "line_user_id,auto_reply_paused", "line_user_id": f"in.({','.join(uids)})",
            "auto_reply_paused": "eq.true", "limit": "1000"})
        paused = {r["line_user_id"] for r in rows}

    # postback actions (routing table exact/prefix) — เช็คก่อน auto_replies
    pb_actions = []
    if any(e["type"] == "postback" for e in repliable):
        pb_actions = await supa.select("postback_actions", params={
            "select": "*", "enabled": "eq.true", "order": "match.asc"})

    def _pick_postback(data: str):
        for pa in pb_actions:
            if pa["match"] == "prefix" and data.startswith(pa["data"]):
                return pa
            if pa["match"] == "exact" and data == pa["data"]:
                return pa
        return None

    name_cache: dict[str, str] = {}
    async def name_of(uid):
        if not uid:
            return None
        if uid not in name_cache:
            p = await line.get_profile(uid)
            name_cache[uid] = (p or {}).get("displayName") or "เพื่อน"
        return name_cache[uid]

    out_msgs = []
    replied_uids: set[str] = set()   # กันตอบซ้ำใน batch เดียว (เช่น ปุ่ม rich menu ส่ง message + postback)
    for e in repliable:
        try:
            uid = e.get("source", {}).get("userId")
            if uid in paused or uid in replied_uids:
                continue

            # ---- postback action (routing table) มาก่อน ----
            if e["type"] == "postback":
                pa = _pick_postback(e.get("postback", {}).get("data", ""))
                if pa and pa.get("messages"):
                    msgs = pa["messages"][:5]
                    if _has_placeholder(msgs):
                        msgs = _personalize(msgs, await name_of(uid))
                    code, _ = await line.reply(e["replyToken"], msgs)
                    if code == 200:
                        replied_uids.add(uid)
                        await supa.update("postback_actions",
                                          {"hits": (pa.get("hits") or 0) + 1, "last_hit_at": NOW()},
                                          {"id": f"eq.{pa['id']}"})
                        for m in msgs:
                            out_msgs.append({"line_user_id": uid, "direction": "out", "by": "postback",
                                             "msg_type": m.get("type"), "text": m.get("text"), "payload": m})
                    continue

            rule = _pick_rule(e, rules)
            if not rule:
                continue
            # user บนเมนู Gemini + กฎที่เจอคือ fallback -> ไม่ตอบ ปล่อยให้ Gemini จัดการ
            if rule.get("trigger") == "fallback" and uid in gemini_uids:
                continue
            nm = await name_of(uid) if _has_placeholder(rule["messages"]) else None
            if await _reply_rule(e["replyToken"], rule, nm):
                replied_uids.add(uid)
                for m in rule["messages"][:5]:
                    out_msgs.append({"line_user_id": uid, "direction": "out", "by": "auto",
                                     "msg_type": m.get("type"), "text": m.get("text"), "payload": m})
        except Exception as ex:
            print("auto-reply one error:", repr(ex))
    if out_msgs:
        try:
            await supa.insert("messages", out_msgs)
        except Exception:
            pass
    return replied_uids


# ============================================================
# dashboard
# ============================================================
def _day(s):
    return (s or "")[:10]


def _day_series(n: int) -> list[str]:
    today = dt.date.today()
    return [(today - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _iso_ago(**kw):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).isoformat()


async def _gather_dict(**coros):
    keys = list(coros.keys())
    vals = await asyncio.gather(*coros.values(), return_exceptions=True)
    return {k: (None if isinstance(v, Exception) else v) for k, v in zip(keys, vals)}


@app.get("/api/dashboard")
async def dashboard(admin=Depends(current_admin), range: int = 30):
    days = max(7, min(range, 90))
    d7, d30, dN = _iso_ago(days=7), _iso_ago(days=30), _iso_ago(days=days)
    today0 = dt.date.today().isoformat()

    # ---------- headline counts (ขนานกันหมด) ----------
    c = await _gather_dict(
        total=supa.count("line_users"),
        following=supa.count("line_users", {"is_following": "eq.true"}),
        not_following=supa.count("line_users", {"is_following": "eq.false"}),
        no_menu=supa.count("line_users", {"is_following": "eq.true", "current_rich_menu_id": "is.null"}),
        active7=supa.count("line_users", {"is_following": "eq.true", "last_message_at": f"gte.{d7}"}),
        active30=supa.count("line_users", {"is_following": "eq.true", "last_message_at": f"gte.{d30}"}),
        unread=supa.count("line_users", {"unread": "gt.0"}),
        new7=supa.count("line_users", {"first_followed_at": f"gte.{d7}"}),
        new30=supa.count("line_users", {"first_followed_at": f"gte.{d30}"}),
        msg_in_7=supa.count("messages", {"direction": "eq.in", "created_at": f"gte.{d7}"}),
        msg_out_7=supa.count("messages", {"direction": "eq.out", "created_at": f"gte.{d7}"}),
        wh_today=supa.count("webhook_events", {"created_at": f"gte.{today0}"}),
        slip_total=supa.count("slips"),
        slip_new=supa.count("slips", {"status": "eq.new"}),
        slip_review=supa.count("slips", {"status": "eq.review"}),
        slip_verified=supa.count("slips", {"status": "eq.verified"}),
        slip_rejected=supa.count("slips", {"status": "eq.rejected"}),
        slip_today=supa.count("slips", {"created_at": f"gte.{today0}"}),
        slip_week=supa.count("slips", {"created_at": f"gte.{d7}"}),
        err24=supa.count("operations", {"status": "eq.error", "created_at": f"gte.{_iso_ago(hours=24)}"}),
        rows_users=supa.count("line_users"),
        rows_msg=supa.count("messages"),
        rows_wh=supa.count("webhook_events"),
        rows_ev_pending=supa.count("automation_runs", {"status": "eq.pending"}),
        msg_today=supa.count("messages", {"created_at": f"gte.{today0}"}),
        wh_7=supa.count("webhook_events", {"created_at": f"gte.{d7}"}),
        gemini_7=supa.count("messages", {"by": "eq.gemini", "created_at": f"gte.{d7}"}),
        ar_total=supa.count("auto_replies"),
        ar_on=supa.count("auto_replies", {"enabled": "eq.true"}),
        pb_total=supa.count("postback_actions"),
        pb_on=supa.count("postback_actions", {"enabled": "eq.true"}),
        seg_total=supa.count("segments"),
        tpl_total=supa.count("message_templates"),
        link_total=supa.count("short_links"),
        menu_total=supa.count("rich_menus"),
        auto_total=supa.count("automations"),
        auto_on=supa.count("automations", {"enabled": "eq.true"}),
        sched_pending=supa.count("scheduled_jobs", {"status": "eq.pending"}),
        blocked=supa.count("line_users", {"block_count": "gt.0", "is_following": "eq.false"}),
        tasks_open=supa.count("tasks", {"status": "eq.open"}),
        tasks_overdue=supa.count("tasks", {"status": "eq.open", "due_at": f"lt.{_iso_ago(days=0)}"}),
        **{f"stg_{s}": supa.count("line_users", {"stage": f"eq.{s}", "is_following": "eq.true"})
           for s in PIPELINE_STAGES},
    )

    # ---------- LINE API ----------
    line_data = await _gather_dict(bot=line.bot_info(), quota=line.message_quota())
    bot = line_data.get("bot") or {"error": "ดึงข้อมูลบอทไม่ได้"}
    quota = line_data.get("quota") or {}

    # ---------- growth: follow_history (real-time) ----------
    fh = await supa.select_all("follow_history", params={"select": "action,is_unblocked,event_ts,created_at"})
    gkeys = _day_series(days)
    gmap = {k: {"day": k, "follow": 0, "unfollow": 0, "unblock": 0} for k in gkeys}
    for r in fh:
        k = _day(r.get("event_ts") or r.get("created_at"))
        if k in gmap:
            if r["action"] == "follow":
                gmap[k]["follow"] += 1
                if r.get("is_unblocked"):
                    gmap[k]["unblock"] += 1
            elif r["action"] == "unfollow":
                gmap[k]["unfollow"] += 1
    growth = []
    run = 0
    for k in gkeys:
        g = gmap[k]
        g["net"] = g["follow"] - g["unfollow"]
        run += g["net"]
        g["cumulative"] = run
        growth.append(g)
    tot_follow = sum(g["follow"] for g in growth)
    tot_unfollow = sum(g["unfollow"] for g in growth)

    # ---------- new followers / day (first_followed_at) ----------
    lu = await supa.select_all("line_users", params={
        "select": "first_followed_at,source,current_rich_menu_id,is_following"})
    nmap = {k: 0 for k in gkeys}
    src_map: dict[str, int] = {}
    menu_count: dict[str, int] = {}
    for u in lu:
        k = _day(u.get("first_followed_at"))
        if k in nmap:
            nmap[k] += 1
        src_map[u.get("source") or "unknown"] = src_map.get(u.get("source") or "unknown", 0) + 1
        if u.get("is_following"):
            mk = u.get("current_rich_menu_id") or "__none__"
            menu_count[mk] = menu_count.get(mk, 0) + 1
    new_daily = [{"day": k, "count": nmap[k]} for k in gkeys]
    source_breakdown = sorted(
        [{"source": k, "count": v} for k, v in src_map.items()], key=lambda x: -x["count"])

    # ---------- menu distribution ----------
    menu_names = {m["rich_menu_id"]: m.get("name") for m in await supa.select(
        "rich_menus", params={"select": "rich_menu_id,name"})}
    try:
        default_menu = await line.richmenu_get_default()
    except Exception:
        default_menu = None
    menu_dist = sorted([{
        "name": "— ไม่มีเมนู —" if k == "__none__" else (menu_names.get(k) or "(เมนูถูกลบ)"),
        "rich_menu_id": None if k == "__none__" else k,
        "count": v, "is_default": k == default_menu,
    } for k, v in menu_count.items()], key=lambda x: -x["count"])

    # ---------- slips: revenue ----------
    slips = await supa.select_all("slips", params={
        "select": "status,amount,expected_course,created_at,reviewed_at"})
    rev_total = sum(float(s.get("amount") or 0) for s in slips if s.get("status") == "verified")
    rev_week = sum(float(s.get("amount") or 0) for s in slips
                   if s.get("status") == "verified" and _day(s.get("reviewed_at") or s.get("created_at")) >= _day(d7))
    course_map: dict[str, dict] = {}
    skeys = _day_series(days)
    sdaily = {k: {"day": k, "verified": 0, "review": 0, "rejected": 0, "new": 0} for k in skeys}
    for s in slips:
        co = s.get("expected_course") or "ไม่ระบุ"
        cm = course_map.setdefault(co, {"course": co, "count": 0, "verified": 0, "amount": 0.0})
        cm["count"] += 1
        if s.get("status") == "verified":
            cm["verified"] += 1
            cm["amount"] += float(s.get("amount") or 0)
        k = _day(s.get("created_at"))
        if k in sdaily and s.get("status") in sdaily[k]:
            sdaily[k][s["status"]] += 1
    slip_courses = sorted(course_map.values(), key=lambda x: -x["amount"])

    # ---------- system: cron health ----------
    ops = await supa.select("operations", params={
        "select": "action,status,created_at", "order": "created_at.desc", "limit": "80"})
    cron_jobs = {}
    for o in ops:
        a = o.get("action") or ""
        if "cron" in a or a.startswith(("richmenu.sync", "automation.run", "stats.snapshot", "backup")):
            base = a.split(".cron")[0]
            if base not in cron_jobs:
                cron_jobs[base] = {"job": base, "last_run": o["created_at"], "status": o.get("status")}
    recent_ops = await supa.select("operations", params={
        "select": "id,actor,action,status,created_at", "order": "created_at.desc", "limit": "10"})
    recent_bc = await supa.select("broadcasts", params={
        "select": "id,kind,target_count,status,created_at", "order": "created_at.desc", "limit": "6"})

    # ---------- สลิปล่าสุด + follow ล่าสุด ----------
    recent_slips = await supa.select("slips", params={
        "select": "id,line_user_id,amount,status,expected_course,auto_note,created_at",
        "order": "created_at.desc", "limit": "8"})
    recent_follows = await supa.select("follow_history", params={
        "select": "line_user_id,action,is_unblocked,event_ts", "order": "created_at.desc", "limit": "10"})
    _uids = list({r["line_user_id"] for r in recent_slips + recent_follows if r.get("line_user_id")})
    _names = {}
    if _uids:
        for r in await supa.select("line_users", params={
                "select": "line_user_id,display_name,picture_url",
                "line_user_id": f"in.({','.join(_uids)})", "limit": "50"}):
            _names[r["line_user_id"]] = r
    for r in recent_slips + recent_follows:
        u = _names.get(r.get("line_user_id"), {})
        r["display_name"] = u.get("display_name")
        r["picture_url"] = u.get("picture_url")

    # quota projection
    qv = (quota.get("quota") or {})
    q_limit = qv.get("value") if isinstance(qv, dict) else None
    q_used = quota.get("totalUsage")
    day_of_month = dt.date.today().day
    q_proj = round(q_used / day_of_month * 30) if (q_used and day_of_month) else None

    return {
        "generated_at": NOW(),
        "range_days": days,
        "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "?")[:7],
        "users": {
            "total": c["total"], "following": c["following"], "not_following": c["not_following"],
            "no_menu": c["no_menu"], "with_menu": (c["following"] or 0) - (c["no_menu"] or 0),
            "active_7d": c["active7"], "active_30d": c["active30"],
            "new_7d": c["new7"], "new_30d": c["new30"], "unread_threads": c["unread"],
            "follow_rate": round((c["following"] or 0) / (c["total"] or 1) * 100),
            "active_rate": round((c["active30"] or 0) / (c["following"] or 1) * 100),
            "source_breakdown": source_breakdown,
            "pipeline": [{"key": s, "label": _STAGE_LABEL.get(s, s), "count": c.get(f"stg_{s}") or 0}
                         for s in PIPELINE_STAGES],
        },
        "growth": growth,
        "growth_totals": {"follow": tot_follow, "unfollow": tot_unfollow,
                          "net": tot_follow - tot_unfollow},
        "new_daily": new_daily,
        "menu_distribution": menu_dist,
        "slips": {
            "total": c["slip_total"], "today": c["slip_today"], "week": c["slip_week"],
            "by_status": {"new": c["slip_new"], "review": c["slip_review"],
                          "verified": c["slip_verified"], "rejected": c["slip_rejected"]},
            "pending_action": (c["slip_new"] or 0) + (c["slip_review"] or 0),
            "revenue_verified": round(rev_total), "revenue_week": round(rev_week),
            "by_course": slip_courses,
            "daily": [sdaily[k] for k in skeys],
        },
        "messages": {"in_7d": c["msg_in_7"], "out_7d": c["msg_out_7"],
                     "today": c["msg_today"], "gemini_7d": c["gemini_7"],
                     "webhook_7d": c["wh_7"]},
        "bot": bot, "quota": quota,
        "counts": {
            "auto_replies": c["ar_total"], "auto_replies_on": c["ar_on"],
            "postbacks": c["pb_total"], "postbacks_on": c["pb_on"],
            "automations": c["auto_total"], "automations_on": c["auto_on"],
            "segments": c["seg_total"], "templates": c["tpl_total"],
            "links": c["link_total"], "rich_menus": c["menu_total"],
            "scheduled_pending": c["sched_pending"], "blocked_ever": c["blocked"],
        },
        "system": {
            "quota_limit": q_limit, "quota_used": q_used,
            "quota_pct": round((q_used or 0) / (q_limit or 1) * 100) if q_limit else None,
            "quota_projected": q_proj,
            "webhook_today": c["wh_today"], "errors_24h": c["err24"],
            "automations_pending": c["rows_ev_pending"],
            "tasks_open": c["tasks_open"], "tasks_overdue": c["tasks_overdue"],
            "cron": sorted(cron_jobs.values(), key=lambda x: x["job"]),
            "db_rows": {"line_users": c["rows_users"], "messages": c["rows_msg"],
                        "webhook_events": c["rows_wh"], "slips": c["slip_total"]},
        },
        "recent_operations": recent_ops,
        "recent_broadcasts": recent_bc,
        "recent_slips": recent_slips,
        "recent_follows": recent_follows,
    }


@app.get("/api/dashboard/analytics")
async def dashboard_analytics(admin=Depends(current_admin), range: int = 30):
    days = max(7, min(range, 90))
    dkeys = _day_series(days)
    dN = _iso_ago(days=days)

    # ---------- messages: type / source / daily / heatmap / top talkers ----------
    msgs = await supa.select_all("messages", params={
        "select": "direction,by,msg_type,created_at,line_user_id", "created_at": f"gte.{dN}"})
    in_daily = {k: 0 for k in dkeys}
    out_daily = {k: 0 for k in dkeys}
    type_map: dict[str, int] = {}
    out_src: dict[str, int] = {}
    talk: dict[str, int] = {}
    # heatmap: 7 weekday rows x 24 hour cols (นับ inbound)
    heat = [[0] * 24 for _ in range(7)]
    for m in msgs:
        k = _day(m.get("created_at"))
        if m.get("direction") == "in":
            if k in in_daily:
                in_daily[k] += 1
            type_map[m.get("msg_type") or "?"] = type_map.get(m.get("msg_type") or "?", 0) + 1
            if m.get("line_user_id"):
                talk[m["line_user_id"]] = talk.get(m["line_user_id"], 0) + 1
            try:
                ts = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
                heat[ts.weekday()][ts.hour] += 1
            except Exception:
                pass
        else:
            if k in out_daily:
                out_daily[k] += 1
            b = m.get("by") or "?"
            b = b if b in ("auto", "manual", "postback", "automation", "system", "broadcast", "gemini") else "admin"
            out_src[b] = out_src.get(b, 0) + 1

    top_talk_ids = sorted(talk, key=lambda u: -talk[u])[:10]
    talk_names = {}
    if top_talk_ids:
        for r in await supa.select("line_users", params={
                "select": "line_user_id,display_name,picture_url",
                "line_user_id": f"in.({','.join(top_talk_ids)})", "limit": "20"}):
            talk_names[r["line_user_id"]] = r
    top_talkers = [{
        "line_user_id": u, "count": talk[u],
        "display_name": (talk_names.get(u) or {}).get("display_name"),
        "picture_url": (talk_names.get(u) or {}).get("picture_url"),
    } for u in top_talk_ids]

    # ---------- webhook events by type / day ----------
    whe = await supa.select_all("webhook_events", params={
        "select": "event_type,created_at", "created_at": f"gte.{dN}"})
    wh_type: dict[str, int] = {}
    wh_daily = {k: 0 for k in dkeys}
    for w in whe:
        wh_type[w.get("event_type") or "?"] = wh_type.get(w.get("event_type") or "?", 0) + 1
        k = _day(w.get("created_at"))
        if k in wh_daily:
            wh_daily[k] += 1

    # ---------- bot: top auto-replies / postbacks ----------
    rules = await supa.select("auto_replies", params={
        "select": "name,trigger,hits,enabled,last_hit_at", "order": "hits.desc", "limit": "12"})
    pbs = await supa.select("postback_actions", params={
        "select": "data,label,hits,enabled,last_hit_at", "order": "hits.desc", "limit": "12"})
    autos = await supa.select("automations", params={"select": "name,enabled,trigger,runs"})
    ar_runs = await supa.select_all("automation_runs", params={"select": "status"})
    ar_stat: dict[str, int] = {}
    for r in ar_runs:
        ar_stat[r.get("status") or "?"] = ar_stat.get(r.get("status") or "?", 0) + 1

    # ---------- links ----------
    links = await supa.select("short_links", params={
        "select": "code,label,clicks,target", "order": "clicks.desc", "limit": "12"})
    lc = await supa.select_all("link_clicks", params={
        "select": "clicked_at", "clicked_at": f"gte.{dN}"})
    lc_daily = {k: 0 for k in dkeys}
    for x in lc:
        k = _day(x.get("clicked_at"))
        if k in lc_daily:
            lc_daily[k] += 1

    # ---------- scheduled upcoming ----------
    sched = await supa.select("scheduled_jobs", params={
        "select": "kind,run_at,label,repeat,status", "status": "eq.pending",
        "order": "run_at.asc", "limit": "8"})

    # ---------- LINE insight (demographic + delivery) ----------
    li = await _gather_dict(
        demo=line.insight_demographic(),
        delivery=line.insight_message_delivery((dt.date.today() - dt.timedelta(days=2)).strftime("%Y%m%d")),
    )
    demo = li.get("demo") or {}
    delivery = li.get("delivery") or {}
    hist = await supa.select("stats_daily", params={
        "select": "day,followers,targeted_reaches,blocks", "order": "day.desc", "limit": str(days)})

    # ---------- funnel ----------
    fu = await _gather_dict(
        total=supa.count("line_users"),
        following=supa.count("line_users", {"is_following": "eq.true"}),
        messaged=supa.count("line_users", {"is_following": "eq.true", "last_message_at": "not.is.null"}),
        slipped=supa.count("slips"),
        verified=supa.count("slips", {"status": "eq.verified"}),
    )

    TYPE_LABEL = {"text": "ข้อความ", "image": "รูป", "sticker": "สติกเกอร์", "video": "วิดีโอ",
                  "audio": "เสียง", "location": "ตำแหน่ง", "file": "ไฟล์"}
    SRC_LABEL = {"auto": "ตอบอัตโนมัติ", "manual": "แอดมินพิมพ์", "admin": "แอดมินพิมพ์",
                 "postback": "ปุ่ม/Postback", "automation": "Automation",
                 "broadcast": "Broadcast", "system": "ระบบ", "gemini": "Gemini AI"}
    return {
        "range_days": days,
        "messages": {
            "in_daily": [{"day": k, "count": in_daily[k]} for k in dkeys],
            "out_daily": [{"day": k, "count": out_daily[k]} for k in dkeys],
            "by_type": sorted([{"type": TYPE_LABEL.get(k, k), "count": v}
                               for k, v in type_map.items()], key=lambda x: -x["count"]),
            "out_by_source": sorted([{"source": SRC_LABEL.get(k, k), "count": v}
                                     for k, v in out_src.items()], key=lambda x: -x["count"]),
            "heatmap": heat,
            "top_talkers": top_talkers,
        },
        "delivery": delivery,
        "webhook": {
            "by_type": sorted([{"type": k, "count": v} for k, v in wh_type.items()], key=lambda x: -x["count"]),
            "daily": [{"day": k, "count": wh_daily[k]} for k in dkeys],
        },
        "bot": {
            "top_rules": rules, "top_postbacks": pbs,
            "automations": autos, "automation_runs": ar_stat,
        },
        "links": {
            "top": links,
            "daily": [{"day": k, "count": lc_daily[k]} for k in dkeys],
        },
        "scheduled": sched,
        "demographic": demo,
        "history": list(reversed(hist)),
        "funnel": [
            {"step": "ผู้ใช้ทั้งหมด", "count": fu["total"] or 0},
            {"step": "กำลังติดตาม", "count": fu["following"] or 0},
            {"step": "เคยทักแชท", "count": fu["messaged"] or 0},
            {"step": "ส่งสลิป", "count": fu["slipped"] or 0},
            {"step": "ยืนยันชำระ", "count": fu["verified"] or 0},
        ],
    }


_GAP_STOP = {"ครับ", "ค่ะ", "คะ", "นะ", "น่ะ", "ที่", "การ", "ของ", "และ", "หรือ", "ไหม", "มั้ย",
             "อยู่", "ได้", "จะ", "ให้", "มา", "ไป", "ว่า", "คือ", "กับ", "ใน", "เป็น", "ก็", "ๆ",
             "ผม", "หนู", "เรา", "ยัง", "แล้ว", "ด้วย", "ค่ะ", "อ่ะ", "อะ", "เอา", "มี", "ไม่",
             "ทำ", "ต้อง", "นี้", "นั้น", "อัน", "ตัว", "เลย", "จ้า", "จ้ะ", "งับ", "hello", "hi"}


# ---------- knowledge base ----------
@app.get("/api/kb")
async def kb_list(admin=Depends(current_admin)):
    try:
        rows = await supa.select("kb_articles", params={"select": "*", "order": "category,title"})
    except Exception:
        return {"articles": [], "schema_missing": True,
                "hint": "รัน migration 0010 (kb_articles) ใน Supabase SQL Editor"}
    return {"articles": rows}


@app.post("/api/kb")
async def kb_save(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    if not (b.get("title") or "").strip() or not (b.get("body") or "").strip():
        raise HTTPException(400, "ต้องมีหัวข้อและเนื้อหา")
    row = {"title": b["title"].strip(), "body": b["body"].strip(),
           "keywords": [k.strip() for k in (b.get("keywords") or []) if k.strip()],
           "category": b.get("category"), "enabled": b.get("enabled", True),
           "updated_by": admin["userId"], "updated_at": NOW()}
    if b.get("id"):
        await supa.update("kb_articles", row, {"id": f"eq.{b['id']}"})
    else:
        await supa.insert("kb_articles", row)
    _gemini_kb_cache["items"] = None
    return {"ok": True}


@app.delete("/api/kb/{aid}")
async def kb_delete(aid: int, admin=Depends(current_admin)):
    await supa.delete("kb_articles", {"id": f"eq.{aid}"})
    _gemini_kb_cache["items"] = None
    return {"ok": True}


@app.get("/api/kb/gaps")
async def kb_gaps(admin=Depends(current_admin), days: int = 14):
    """คำถามที่ Gemini ตอบแล้วแต่ไม่เจอบทความ/FAQ ที่เกี่ยวข้องเลยในคลังความรู้ (gemini.kb_gap log)
    ใช้ดูว่าควรเพิ่มบทความอะไรเข้า /kb"""
    days = max(3, min(days, 60))
    since = _iso_ago(days=days)
    try:
        rows = await supa.select_all("operations", params={
            "select": "params,created_at", "action": "eq.gemini.kb_gap",
            "created_at": f"gte.{since}"})
    except Exception:
        rows = []
    from collections import Counter
    texts = [re.sub(r"\s+", " ", str((r.get("params") or {}).get("q") or "").strip()) for r in rows]
    texts = [t for t in texts if t]
    exact = Counter(t.lower() for t in texts)
    top_questions = [{"text": k, "count": v} for k, v in exact.most_common(40)]
    words = Counter()
    for t in texts:
        for w in re.findall(r"[ก-๙a-zA-Z]{2,}", t.lower()):
            if w not in _GAP_STOP and len(w) >= 2:
                words[w] += 1
    top_keywords = [{"word": w, "count": c} for w, c in words.most_common(30) if c >= 2]
    return {"days": days, "total": len(texts),
            "top_questions": top_questions, "top_keywords": top_keywords, "samples": texts[:60]}


@app.get("/api/insights/gaps")
async def insight_gaps(admin=Depends(current_admin), days: int = 14):
    """ข้อความที่ลูกค้าพิมพ์มาแต่ไม่ตรงกฎ auto-reply ไหนเลย — จัดกลุ่มให้เห็นว่าควรสร้างกฎอะไร"""
    days = max(3, min(days, 60))
    since = _iso_ago(days=days)
    msgs = await supa.select_all("messages", params={
        "select": "text,created_at", "direction": "eq.in", "msg_type": "eq.text",
        "created_at": f"gte.{since}"})
    rules = await supa.select("auto_replies", params={
        "select": "keywords,match_type", "enabled": "eq.true", "trigger": "eq.text", "limit": "300"})

    def _has_rule(text: str) -> bool:
        for r in rules:
            if _match({"keywords": r.get("keywords"), "match_type": r.get("match_type", "contains")}, text):
                return True
        return False

    from collections import Counter
    unmatched = [re.sub(r"\s+", " ", (m.get("text") or "").strip())
                 for m in msgs if m.get("text") and m["text"].strip()]
    unmatched = [t for t in unmatched if not _has_rule(t)]

    exact = Counter(t.lower()[:140] for t in unmatched if 2 <= len(t) <= 200)
    top_questions = [{"text": k, "count": v} for k, v in exact.most_common(40) if v >= 2]

    words = Counter()
    for t in unmatched:
        for w in re.findall(r"[ก-๙a-zA-Z]{2,}", t.lower()):
            if w not in _GAP_STOP and len(w) >= 2:
                words[w] += 1
    top_keywords = [{"word": w, "count": c} for w, c in words.most_common(30) if c >= 3]

    return {
        "days": days,
        "total_in_text": len(msgs),
        "unmatched": len(unmatched),
        "unmatched_pct": round(len(unmatched) / (len(msgs) or 1) * 100),
        "top_questions": top_questions,
        "top_keywords": top_keywords,
        "samples": unmatched[:60],
    }


# ============================================================
# RICH MENU
# ============================================================
@app.get("/api/richmenus")
async def richmenus(admin=Depends(current_admin)):
    menus = await line.richmenu_list()
    default_id = await line.richmenu_get_default()
    aliases = await line.richmenu_alias_list()
    # cache ลง DB
    try:
        await supa.upsert("rich_menus", [{
            "rich_menu_id": m["richMenuId"], "name": m.get("name"),
            "chat_bar_text": m.get("chatBarText"), "selected": m.get("selected"),
            "size": m.get("size"), "areas": m.get("areas"),
            "is_default": m["richMenuId"] == default_id, "synced_at": NOW(),
        } for m in menus], on_conflict="rich_menu_id")
    except Exception:
        pass
    # แนบค่า gemini_enabled/gemini_temperature ของแต่ละเมนู (ตั้งจากหน้าเว็บ) เข้าไปในผลลัพธ์
    try:
        gsettings = await supa.select("rich_menus", params={
            "select": "rich_menu_id,gemini_enabled,gemini_temperature"})
        gmap = {g["rich_menu_id"]: g for g in gsettings}
    except Exception:
        gmap = {}
    for m in menus:
        g = gmap.get(m["richMenuId"]) or {}
        m["geminiEnabled"] = bool(g.get("gemini_enabled"))
        try:
            m["geminiTemperature"] = float(g.get("gemini_temperature") or 0.7)
        except (TypeError, ValueError):
            m["geminiTemperature"] = 0.7
    return {"menus": menus, "defaultRichMenuId": default_id, "aliases": aliases}


@app.post("/api/richmenu/{rid}/gemini")
async def set_menu_gemini(rid: str, req: Request, admin=Depends(current_admin)):
    """เปิด/ปิด Gemini + ตั้งความเข้มข้นของคำตอบ (temperature) แยกตามเมนู"""
    b = await req.json()
    enabled = bool(b.get("enabled"))
    try:
        temperature = float(b.get("temperature", 0.7))
    except (TypeError, ValueError):
        raise HTTPException(400, "temperature ต้องเป็นตัวเลข")
    temperature = max(0.0, min(2.0, temperature))
    await supa.update("rich_menus",
                       {"gemini_enabled": enabled, "gemini_temperature": temperature},
                       params={"rich_menu_id": f"eq.{rid}"})
    await supa.log_operation(admin["userId"], "richmenu.gemini_settings",
                             {"richMenuId": rid, "enabled": enabled, "temperature": temperature},
                             {"ok": True})
    return {"ok": True, "richMenuId": rid, "enabled": enabled, "temperature": temperature}


@app.get("/api/richmenu/usage")
async def richmenu_usage(admin=Depends(current_admin)):
    """นับจำนวน user ที่ผูกแต่ละ rich menu อยู่ (จาก DB) เรียงมาก→น้อย"""
    rows = await supa.select_all("line_users", params={
        "select": "current_rich_menu_id", "is_following": "eq.true",
    })
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("current_rich_menu_id") or "__none__"
        counts[k] = counts.get(k, 0) + 1

    names = {m["richMenuId"]: m.get("name") for m in await line.richmenu_list()}
    try:
        default_id = await line.richmenu_get_default()
    except Exception:
        default_id = None

    items = [{
        "richMenuId": None if k == "__none__" else k,
        "name": "— ไม่มีเมนู —" if k == "__none__" else (names.get(k) or "(เมนูถูกลบแล้ว)"),
        "count": v,
        "isDefault": k == default_id,
        "exists": k == "__none__" or k in names,
    } for k, v in counts.items()]
    items.sort(key=lambda x: -x["count"])
    return {"total": len(rows), "items": items, "defaultRichMenuId": default_id}


@app.post("/api/richmenu/default")
async def set_default(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    rid = b.get("richMenuId")
    if not rid:
        raise HTTPException(400, "ต้องระบุ richMenuId")
    ok, txt = await line.richmenu_set_default(rid)
    await supa.log_operation(admin["userId"], "richmenu.set_default", {"richMenuId": rid},
                             {"ok": ok, "resp": txt[:200]}, "ok" if ok else "error")
    if not ok:
        raise HTTPException(400, txt)
    return {"ok": True}


@app.delete("/api/richmenu/default")
async def clear_default(admin=Depends(current_admin)):
    ok = await line.richmenu_clear_default()
    await supa.log_operation(admin["userId"], "richmenu.clear_default", None, {"ok": ok})
    return {"ok": ok}


@app.post("/api/richmenu/create")
async def create_menu(req: Request, admin=Depends(current_admin)):
    """สร้าง rich menu + (ถ้าส่ง imageBase64 มา) อัปโหลดรูปให้เลย"""
    b = await req.json()
    payload = b.get("richMenu") or {k: v for k, v in b.items() if k != "imageBase64"}
    menu = await line.richmenu_create(payload)
    rid = menu.get("richMenuId")

    img = b.get("imageBase64")
    if img and rid:
        if "," in img:
            header, img = img.split(",", 1)
            ctype = "image/jpeg" if "jpeg" in header or "jpg" in header else "image/png"
        else:
            ctype = "image/png"
        content = base64.b64decode(img)
        ok, txt = await line.richmenu_upload_image(rid, content, ctype)
        if not ok:
            await line.richmenu_delete(rid)
            raise HTTPException(400, f"อัปโหลดรูปไม่ผ่าน: {txt[:200]}")

    if b.get("setDefault") and rid:
        await line.richmenu_set_default(rid)
    await supa.log_operation(admin["userId"], "richmenu.create", payload, menu)
    return menu


@app.post("/api/richmenu/{rid}/image")
async def upload_menu_image(rid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    img = b.get("imageBase64", "")
    if "," in img:
        header, img = img.split(",", 1)
        ctype = "image/jpeg" if "jpeg" in header or "jpg" in header else "image/png"
    else:
        ctype = "image/png"
    ok, txt = await line.richmenu_upload_image(rid, base64.b64decode(img), ctype)
    if not ok:
        raise HTTPException(400, txt)
    return {"ok": True}


@app.delete("/api/richmenu/{rid}")
async def delete_menu(rid: str, admin=Depends(current_admin)):
    ok = await line.richmenu_delete(rid)
    await supa.log_operation(admin["userId"], "richmenu.delete", {"richMenuId": rid}, {"ok": ok})
    return {"ok": ok}


@app.post("/api/richmenu/alias")
async def create_alias(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    ok, txt = await line.richmenu_alias_create(b["richMenuAliasId"], b["richMenuId"])
    if not ok:
        raise HTTPException(400, txt)
    return {"ok": True}


@app.delete("/api/richmenu/alias/{alias_id}")
async def delete_alias(alias_id: str, admin=Depends(current_admin)):
    return {"ok": await line.richmenu_alias_delete(alias_id)}


def _cron_authorized(request: Request) -> bool:
    """รองรับทั้ง ?key=, header x-cron-key และ Authorization: Bearer <CRON_SECRET>
    (Vercel Cron จะแนบ Authorization: Bearer <CRON_SECRET> ให้อัตโนมัติถ้าตั้ง env CRON_SECRET ไว้)"""
    if not CRON_SECRET:
        return True
    key = request.query_params.get("key") or request.headers.get("x-cron-key", "")
    auth = request.headers.get("authorization", "")
    return key == CRON_SECRET or auth == f"Bearer {CRON_SECRET}"


async def _fetch_current_menu_map(uids: list[str]) -> dict:
    """ดึงค่าปัจจุบันใน DB ของ uids ก่อนเขียนทับ ใช้เทียบหา diff สำหรับ richmenu_history"""
    out: dict = {}
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        rows = await supa.select("line_users", params={
            "select": "line_user_id,current_rich_menu_id,rich_menu_status",
            "line_user_id": f"in.({','.join(chunk)})",
        })
        for r in rows:
            out[r["line_user_id"]] = r
    return out


async def _log_richmenu_diffs(old_map: dict, new_rows: list[dict], source: str, actor: str | None):
    """เทียบ old vs new แล้วบันทึกเฉพาะแถวที่เปลี่ยนจริงลง richmenu_history"""
    history_rows = []
    for row in new_rows:
        uid = row["line_user_id"]
        old = old_map.get(uid, {})
        old_rid, old_status = old.get("current_rich_menu_id"), old.get("rich_menu_status")
        new_rid, new_status = row.get("current_rich_menu_id"), row.get("rich_menu_status")
        if old_rid != new_rid or old_status != new_status:
            history_rows.append({
                "line_user_id": uid, "old_rich_menu_id": old_rid, "new_rich_menu_id": new_rid,
                "old_status": old_status, "new_status": new_status,
                "source": source, "actor": actor,
            })
    if history_rows:
        try:
            for i in range(0, len(history_rows), 500):
                await supa.insert("richmenu_history", history_rows[i:i + 500])
        except Exception as e:
            print("richmenu_history insert error:", e)
    return len(history_rows)


@app.post("/api/richmenu/assign")
async def assign(req: Request, admin=Depends(current_admin)):
    """bulk assign: mode = 'link' (default/link/unlink) + target = 'all'|'none'|'list'|'tag'"""
    b = await req.json()
    rid = b.get("richMenuId")
    mode = b.get("mode", "link")           # link | unlink
    target = b.get("target", "list")       # list | all | none | tag
    set_default_too = b.get("setDefault", False)

    if mode == "link" and not rid:
        raise HTTPException(400, "ต้องระบุ richMenuId")

    # หา userIds
    if target == "list":
        uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
    elif target == "segment":
        seg = await supa.select("segments", params={"id": f"eq.{b.get('segmentId')}", "select": "filter", "limit": "1"})
        uids = await _uids_by_filter(seg[0]["filter"] if seg else {})
    elif target == "filter":
        uids = await _uids_by_filter(b.get("filter", {}))
    elif target == "none":
        uids = await _uids_by_filter({"noMenu": True})
    elif target == "tag":
        uids = await _uids_by_filter({"tag": b.get("tag", "")})
    else:  # all
        uids = await _uids_by_filter({})

    if not uids:
        raise HTTPException(400, "ไม่มี userId ปลายทาง")

    if set_default_too and rid:
        await line.richmenu_set_default(rid)

    # ยิงแบบ concurrent มี retry
    sem = asyncio.Semaphore(8)
    results = {"ok": 0, "fail": 0, "errors": []}

    async def one(uid):
        async with sem:
            if mode == "unlink":
                ok = await line.user_richmenu_unlink(uid)
                code = 200 if ok else 0
            else:
                ok, code = await line.user_richmenu_link(uid, rid)
            if ok:
                results["ok"] += 1
            else:
                results["fail"] += 1
                if len(results["errors"]) < 50:
                    results["errors"].append({"userId": uid, "code": code})
            return uid, ok

    old_map = await _fetch_current_menu_map(uids)
    done = await asyncio.gather(*[one(u) for u in uids])

    # อัปเดต DB
    ts = NOW()
    patch_rows = [{
        "line_user_id": u, "is_following": True,
        "current_rich_menu_id": (rid if (ok and mode == "link") else None),
        "rich_menu_status": ("assigned" if (ok and mode == "link") else "none"),
        "rich_menu_checked_at": ts, "updated_at": ts,
    } for u, ok in done]
    try:
        await supa.upsert("line_users", patch_rows, on_conflict="line_user_id")
    except Exception as e:
        results["db_error"] = str(e)

    await _log_richmenu_diffs(old_map, patch_rows, mode, admin["userId"])

    await supa.log_operation(admin["userId"], f"richmenu.{mode}",
                             {"richMenuId": rid, "target": target, "count": len(uids)}, results,
                             "ok" if results["fail"] == 0 else "partial")
    return {"total": len(uids), **results}


async def _sync_richmenu_for(uids: list[str], source: str = "sync", actor: str | None = None) -> dict:
    if not uids:
        return {"total": 0, "assigned": 0, "none": 0, "error": 0}
    old_map = await _fetch_current_menu_map(uids)
    menus = {m["richMenuId"]: m.get("name") for m in await line.richmenu_list()}
    sem = asyncio.Semaphore(10)
    summary = {"assigned": 0, "none": 0, "error": 0}
    patch = []

    async def one(uid):
        async with sem:
            try:
                rid = await line.user_richmenu_get(uid)
                st = "assigned" if rid else "none"
            except Exception:
                rid, st = None, "error"
            summary[st] = summary.get(st, 0) + 1
            patch.append({
                "line_user_id": uid, "current_rich_menu_id": rid or None,
                "rich_menu_name": menus.get(rid) if rid else None,
                "rich_menu_status": st, "rich_menu_checked_at": NOW(), "updated_at": NOW(),
            })

    await asyncio.gather(*[one(u) for u in uids])
    try:
        for i in range(0, len(patch), 500):
            await supa.upsert("line_users", patch[i:i + 500], on_conflict="line_user_id")
    except Exception as e:
        summary["db_error"] = str(e)
    summary["history_logged"] = await _log_richmenu_diffs(old_map, patch, source, actor)
    return {"total": len(uids), **summary}


@app.post("/api/richmenu/sync")
async def richmenu_sync(req: Request, admin=Depends(current_admin)):
    """เช็ค richmenu ปัจจุบันของ user แล้วบันทึกลง DB"""
    b = await req.json()
    if b.get("target") == "list":
        uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
    else:
        rows = await supa.select_all("line_users", params={
            "select": "line_user_id", "is_following": "eq.true",
        })
        uids = [r["line_user_id"] for r in rows]
    if not uids:
        raise HTTPException(400, "ไม่มี user ให้ sync")
    summary = await _sync_richmenu_for(uids, source="sync", actor=admin["userId"])
    await supa.log_operation(admin["userId"], "richmenu.sync", {"count": len(uids)}, summary)
    return summary


@app.api_route("/api/cron/sync-richmenu", methods=["GET", "POST"])
async def cron_sync_richmenu(request: Request):
    """เรียกโดย scheduler (Supabase pg_cron) — auth ด้วย ?key=CRON_SECRET
    sync แบบ rolling: เอา user ที่ถูกเช็คนานสุดก่อน batch ละ ?limit (default 1500)"""
    if not _cron_authorized(request):
        raise HTTPException(403, "bad cron key")
    try:
        limit = min(int(request.query_params.get("limit", "1000")), 1000)  # 1 หน้า PostgREST, พอดี < 60s
    except ValueError:
        limit = 1000

    rows = await supa.select("line_users", params={
        "select": "line_user_id", "is_following": "eq.true",
        "order": "rich_menu_checked_at.asc.nullsfirst",
        "limit": str(limit),
    })
    uids = [r["line_user_id"] for r in rows]
    summary = await _sync_richmenu_for(uids, source="cron", actor="cron")
    await supa.log_operation("cron", "richmenu.sync.cron", {"limit": limit}, summary)
    return {"ok": True, **summary}


@app.get("/api/richmenu/history")
async def richmenu_history(request: Request, admin=Depends(current_admin)):
    """ประวัติการเปลี่ยน rich menu — ?userId= กรองรายคน, ?limit=&offset= pagination"""
    uid = (request.query_params.get("userId") or "").strip()
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = 100, 0
    params = {"select": "*", "order": "created_at.desc", "limit": str(limit), "offset": str(offset)}
    count_params = {}
    if uid:
        params["line_user_id"] = f"eq.{uid}"
        count_params["line_user_id"] = f"eq.{uid}"
    try:
        rows = await supa.select("richmenu_history", params=params)
        total = await supa.count("richmenu_history", count_params)
    except Exception as e:
        # ตาราง richmenu_history ยังไม่ถูกสร้าง (migration 0003 ยังไม่รัน)
        return {"items": [], "total": 0, "limit": limit, "offset": offset,
                "schema_missing": True,
                "hint": "ยังไม่ได้สร้างตาราง richmenu_history — รัน migration 0003 ใน Supabase SQL Editor"}
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.api_route("/api/cron/enforce-richmenu", methods=["GET", "POST"])
async def cron_enforce_richmenu(request: Request):
    """(ปิดโดย default) บังคับ user ที่ current_rich_menu_id ไม่ตรงเป้าหมายให้ผูกกลับเป็น ENFORCE_RICHMENU_ID
    ยกเว้น user ที่อยู่ในเมนูที่ระบุใน ENFORCE_EXCLUDE_MENUS (เช่นเมนูแคมเปญที่ตั้งใจให้อยู่ต่อ)
    เปิดใช้งานโดยตั้ง env ENFORCE_RICHMENU_ID — ถ้าไม่ตั้ง endpoint นี้จะไม่ทำอะไรเลย (no-op ปลอดภัย)"""
    if not _cron_authorized(request):
        raise HTTPException(403, "bad cron key")
    if not ENFORCE_RICHMENU_ID:
        return {"ok": True, "skipped": "ENFORCE_RICHMENU_ID ไม่ได้ตั้งค่า — enforce ปิดอยู่"}
    try:
        limit = min(int(request.query_params.get("limit", "500")), 1000)
    except ValueError:
        limit = 500

    rows = await supa.select("line_users", params={
        "select": "line_user_id,current_rich_menu_id", "is_following": "eq.true",
        "current_rich_menu_id": f"not.eq.{ENFORCE_RICHMENU_ID}",
        "order": "rich_menu_checked_at.asc.nullsfirst",
        "limit": str(limit),
    })
    uids = [r["line_user_id"] for r in rows
            if (r.get("current_rich_menu_id") or "") not in ENFORCE_EXCLUDE_MENUS]
    if not uids:
        return {"ok": True, "total": 0, "assigned": 0, "fail": 0}

    old_map = await _fetch_current_menu_map(uids)
    sem = asyncio.Semaphore(8)
    results = {"ok": 0, "fail": 0}

    async def one(uid):
        async with sem:
            ok, code = await line.user_richmenu_link(uid, ENFORCE_RICHMENU_ID)
            results["ok" if ok else "fail"] += 1
            return uid, ok

    done = await asyncio.gather(*[one(u) for u in uids])
    ts = NOW()
    patch = [{
        "line_user_id": u,
        "current_rich_menu_id": ENFORCE_RICHMENU_ID if ok else old_map.get(u, {}).get("current_rich_menu_id"),
        "rich_menu_status": "assigned" if ok else old_map.get(u, {}).get("rich_menu_status"),
        "rich_menu_checked_at": ts, "updated_at": ts,
    } for u, ok in done]
    try:
        await supa.upsert("line_users", patch, on_conflict="line_user_id")
    except Exception as e:
        results["db_error"] = str(e)

    await _log_richmenu_diffs(old_map, patch, "enforce", "cron")
    await supa.log_operation("cron", "richmenu.enforce.cron",
                             {"richMenuId": ENFORCE_RICHMENU_ID, "limit": limit}, results,
                             "ok" if results["fail"] == 0 else "partial")
    return {"ok": True, "total": len(uids), "assigned": results["ok"], "fail": results["fail"]}


# ============================================================
# USERS
# ============================================================
# ---------- custom field defs ----------
@app.get("/api/fields")
async def list_fields(admin=Depends(current_admin)):
    return {"fields": await supa.select("field_defs", params={"select": "*", "order": "sort,key"})}


@app.post("/api/fields")
async def save_field(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    key = re.sub(r"[^a-z0-9_]", "", (b.get("key") or "").lower())
    if not key:
        raise HTTPException(400, "key ต้องเป็น a-z 0-9 _")
    await supa.upsert("field_defs", {
        "key": key, "label": b.get("label") or key, "type": b.get("type", "text"),
        "options": b.get("options", []), "sort": int(b.get("sort", 0)),
    }, on_conflict="key")
    return {"ok": True, "key": key}


@app.delete("/api/fields/{key}")
async def del_field(key: str, admin=Depends(current_admin)):
    await supa.delete("field_defs", {"key": f"eq.{key}"})
    return {"ok": True}


@app.get("/api/users")
async def users(admin=Depends(current_admin), limit: int = 100, offset: int = 0,
                q: str = "", following: str = "", menu: str = "", tag: str = ""):
    params = {
        "select": "line_user_id,display_name,picture_url,is_following,current_rich_menu_id,"
                  "rich_menu_name,rich_menu_status,source,tags,note,custom,updated_at",
        "order": "updated_at.desc",
        "limit": str(min(limit, 1000)), "offset": str(offset),
    }
    if q:
        params["or"] = f"(line_user_id.ilike.*{q}*,display_name.ilike.*{q}*,note.ilike.*{q}*)"
    if following in ("true", "false"):
        params["is_following"] = f"eq.{following}"
    if menu == "none":
        params["current_rich_menu_id"] = "is.null"
    elif menu:
        params["current_rich_menu_id"] = f"eq.{menu}"
    if tag:
        params["tags"] = "cs.{" + tag + "}"
    rows = await supa.select("line_users", params=params, headers={"Prefer": "count=exact"})
    total = await supa.count("line_users", {k: v for k, v in params.items()
                                            if k in ("is_following", "current_rich_menu_id", "or", "tags")})
    return {"users": rows, "total": total, "limit": limit, "offset": offset}


@app.post("/api/users/bulk-tag")
async def users_bulk_tag(req: Request, admin=Depends(current_admin)):
    """ใส่/ลบ tag หลายคนพร้อมกัน — target: list | filter | segment"""
    b = await req.json()
    add = [t.strip() for t in b.get("add", []) if t.strip()]
    remove = set(t.strip() for t in b.get("remove", []) if t.strip())
    if b.get("target") == "segment":
        seg = await supa.select("segments", params={"id": f"eq.{b.get('segmentId')}", "select": "filter", "limit": "1"})
        uids = await _uids_by_filter(seg[0]["filter"] if seg else {})
    elif b.get("target") == "filter":
        uids = await _uids_by_filter(b.get("filter", {}))
    else:
        uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
    if not uids:
        raise HTTPException(400, "ไม่มีปลายทาง")

    n = 0
    newly: dict[str, list] = {}
    for i in range(0, len(uids), 400):
        chunk = uids[i:i + 400]
        rows = await supa.select("line_users", params={
            "select": "line_user_id,tags", "line_user_id": f"in.({','.join(chunk)})", "limit": "500"})
        patch = []
        for r in rows:
            cur = set(r.get("tags") or [])
            new = (cur | set(add)) - remove
            if new != cur:
                patch.append({"line_user_id": r["line_user_id"], "tags": sorted(new), "updated_at": NOW()})
                for tg in (set(add) - cur):
                    newly.setdefault(tg, []).append(r["line_user_id"])
        if patch:
            await supa.upsert("line_users", patch, on_conflict="line_user_id")
            n += len(patch)
    for tg, tg_uids in newly.items():
        try:
            await _enroll_automations("tag_added", tg_uids, {"tags": [tg]})
        except Exception as e:
            print("tag_added enroll error:", e)
    await supa.log_operation(admin["userId"], "users.bulk_tag", {"count": len(uids), "add": add, "remove": list(remove)}, {"changed": n})
    return {"ok": True, "target": len(uids), "changed": n}


@app.get("/api/export/users")
async def export_users(admin=Depends(current_admin), following: str = "", tag: str = ""):
    f = {}
    if following in ("true", "false"):
        f["following"] = following
    if tag:
        f["tags"] = [tag]
    else:
        f["following"] = f.get("following", None)
    params = {"select": "line_user_id,display_name,status_message,language,is_following,"
                        "current_rich_menu_id,rich_menu_name,rich_menu_status,source,tags,note,"
                        "custom,follow_count,block_count,first_followed_at,last_message_at,updated_at"}
    if following in ("true", "false"):
        params["is_following"] = f"eq.{following}"
    if tag:
        params["tags"] = "cs.{" + tag + "}"
    rows = await supa.select_all("line_users", params=params)
    fields = await supa.select("field_defs", params={"select": "key,label", "order": "sort"})
    cols = ["line_user_id", "display_name", "status_message", "language", "is_following",
            "rich_menu_name", "rich_menu_status", "source", "tags", "note",
            "follow_count", "block_count", "first_followed_at", "last_message_at", "updated_at"]
    ck = [f["key"] for f in fields]
    header = cols + [f"custom.{k}" for k in ck]

    def esc(v):
        if v is None:
            return ""
        if isinstance(v, list):
            v = "|".join(map(str, v))
        s = str(v)
        return f'"{s.replace(chr(34), chr(34) * 2)}"' if any(c in s for c in ',"\n') else s

    lines = [",".join(header)]
    for r in rows:
        row = [esc(r.get(c)) for c in cols] + [esc((r.get("custom") or {}).get(k)) for k in ck]
        lines.append(",".join(row))
    from fastapi.responses import Response
    return Response("﻿" + "\r\n".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="users-{dt.date.today()}.csv"'})


@app.post("/api/users/import")
async def users_import(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    raw = b.get("userIds", [])
    tag = b.get("tag")
    note = b.get("note")
    uids, seen = [], set()
    for u in raw:
        u = str(u).strip().split(",")[0]
        if u.startswith("U") and len(u) == 33 and u not in seen:
            seen.add(u)
            uids.append(u)
    if not uids:
        raise HTTPException(400, "ไม่พบ userId ที่ถูกต้อง (U + 32 hex)")

    existing = set()
    for i in range(0, len(uids), 800):
        chunk = uids[i:i + 800]
        existing |= {r["line_user_id"] for r in await supa.select("line_users", params={
            "select": "line_user_id", "line_user_id": f"in.({','.join(chunk)})", "limit": "1000",
        })}
    new = [u for u in uids if u not in existing]
    ts = NOW()
    rows = [{
        "line_user_id": u, "source": "import", "is_following": True,
        "tags": [tag] if tag else [], "note": note, "updated_at": ts,
    } for u in new]
    if rows:
        for i in range(0, len(rows), 500):
            await supa.upsert("line_users", rows[i:i + 500], on_conflict="line_user_id")
    await supa.log_operation(admin["userId"], "users.import",
                             {"submitted": len(raw)}, {"new": len(new), "dup": len(uids) - len(new)})
    return {"submitted": len(raw), "valid": len(uids), "new": len(new),
            "duplicate": len(uids) - len(new), "duplicates": list(existing & set(uids))}


def _clean_str(s):
    if not s:
        return s
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 0x20)
    return s or None


@app.post("/api/users/refresh-profile")
async def refresh_profile(req: Request, admin=Depends(current_admin)):
    """ดึง displayName/รูป/statusMessage/ภาษา จาก LINE
    target: 'list' (userIds), 'missing' (ที่ยังไม่มีชื่อ, default), 'all' (ทุกคน)
    เรียกซ้ำจนกว่า remaining = 0 (batch ละ ~limit คน)"""
    b = await req.json()
    target = b.get("target", "missing")
    limit = min(int(b.get("limit", 400)), 800)

    if target == "list":
        uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
        remaining_after = 0
    else:
        params = {"select": "line_user_id", "is_following": "eq.true",
                  "order": "updated_at.asc", "limit": str(limit)}
        if target == "missing":
            params["display_name"] = "is.null"
        rows = await supa.select("line_users", params=params)
        uids = [r["line_user_id"] for r in rows]
        remaining_after = 0
        if target == "missing":
            remaining_after = max(0, await supa.count(
                "line_users", {"is_following": "eq.true", "display_name": "is.null"}) - len(uids))

    sem = asyncio.Semaphore(12)
    got = [0]
    unfollow = [0]
    patch = []

    async def one(uid):
        async with sem:
            p = await line.get_profile(uid)
            if p:
                got[0] += 1
                patch.append({
                    "line_user_id": uid,
                    "display_name": _clean_str(p.get("displayName")),
                    "picture_url": _clean_str(p.get("pictureUrl")),
                    "status_message": _clean_str(p.get("statusMessage")),
                    "language": _clean_str(p.get("language")),
                    "is_following": True, "updated_at": NOW(),
                })
            else:
                unfollow[0] += 1
                patch.append({"line_user_id": uid, "is_following": False, "updated_at": NOW()})

    await asyncio.gather(*[one(u) for u in uids])
    for i in range(0, len(patch), 400):
        await supa.upsert("line_users", patch[i:i + 400], on_conflict="line_user_id")
    await supa.log_operation(admin["userId"], "users.refresh_profile",
                             {"count": len(uids)}, {"got": got[0], "unfollow": unfollow[0]})
    return {"processed": len(uids), "profiles_fetched": got[0],
            "not_following": unfollow[0], "remaining": remaining_after}


@app.post("/api/users/sync-followers")
async def sync_followers(admin=Depends(current_admin)):
    """ต้องเป็น Verified/Premium OA — วน /followers/ids"""
    ids, cursor, pages = [], None, 0
    try:
        while True:
            data = await line.followers_ids(1000, cursor)
            ids.extend(data.get("userIds", []))
            pages += 1
            cursor = data.get("next")
            if not cursor or pages > 60:
                break
    except Exception as e:
        raise HTTPException(400, f"followers/ids ใช้ไม่ได้ (ต้อง Verified/Premium OA): {e}")

    ts = NOW()
    rows = [{"line_user_id": u, "source": "followers_api", "is_following": True, "updated_at": ts}
            for u in dict.fromkeys(ids)]
    for i in range(0, len(rows), 500):
        await supa.upsert("line_users", rows[i:i + 500], on_conflict="line_user_id")
    await supa.log_operation(admin["userId"], "users.sync_followers", None, {"count": len(rows)})
    return {"followers": len(rows), "pages": pages}


# ============================================================
# INBOX (แชต + human takeover)
# ============================================================
@app.get("/api/inbox")
async def inbox_list(admin=Depends(current_admin), limit: int = 40, offset: int = 0,
                     filter: str = "all"):
    params = {
        "select": "line_user_id,display_name,picture_url,last_message_at,last_message_text,"
                  "unread,auto_reply_paused,assigned_to,is_following",
        "order": "last_message_at.desc.nullslast",
        "limit": str(min(limit, 100)), "offset": str(offset),
        "last_message_at": "not.is.null",
    }
    if filter == "unread":
        params["unread"] = "gt.0"
    elif filter == "mine":
        params["assigned_to"] = f"eq.{admin['userId']}"
    elif filter == "paused":
        params["auto_reply_paused"] = "eq.true"
    rows = await supa.select("line_users", params=params)
    total_unread = await supa.count("line_users", {"unread": "gt.0"})
    return {"conversations": rows, "total_unread": total_unread}


@app.get("/api/inbox/{uid}")
async def inbox_thread(uid: str, admin=Depends(current_admin), before: str = "", limit: int = 50):
    urows = await supa.select("line_users", params={
        "select": "line_user_id,display_name,picture_url,status_message,is_following,"
                  "auto_reply_paused,assigned_to,current_rich_menu_id,rich_menu_name,tags,note,unread",
        "line_user_id": f"eq.{uid}", "limit": "1"})
    if not urows:
        raise HTTPException(404, "ไม่พบผู้ใช้")
    params = {"select": "*", "line_user_id": f"eq.{uid}",
              "order": "created_at.desc", "limit": str(min(limit, 100))}
    if before:
        params["created_at"] = f"lt.{before}"
    msgs = await supa.select("messages", params=params)
    msgs.reverse()
    return {"user": urows[0], "messages": msgs}


@app.post("/api/inbox/{uid}/read")
async def inbox_read(uid: str, admin=Depends(current_admin)):
    await supa.update("line_users", {"unread": 0}, {"line_user_id": f"eq.{uid}"})
    return {"ok": True}


@app.post("/api/inbox/{uid}/pause")
async def inbox_pause(uid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    paused = bool(b.get("paused", True))
    await supa.update("line_users", {
        "auto_reply_paused": paused,
        "assigned_to": admin["userId"] if paused else None,
    }, {"line_user_id": f"eq.{uid}"})
    await supa.insert("messages", {
        "line_user_id": uid, "direction": "out", "by": "system", "msg_type": "note",
        "text": f"— {'ปิด' if paused else 'เปิด'}ตอบอัตโนมัติ โดย {admin['name'] or admin['userId'][:8]} —",
    })
    return {"ok": True, "paused": paused}


@app.post("/api/inbox/{uid}/assign")
async def inbox_assign(uid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    await supa.update("line_users", {"assigned_to": b.get("to") or None}, {"line_user_id": f"eq.{uid}"})
    return {"ok": True}


@app.post("/api/inbox/{uid}/send")
async def inbox_send(uid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    msgs = _normalize_messages(b.get("messages", []))
    code, txt, rid = await line.push(uid, msgs)
    if code != 200:
        raise HTTPException(400, txt)
    rows = [{"line_user_id": uid, "direction": "out", "by": admin["userId"],
             "msg_type": m.get("type"), "text": m.get("text"), "payload": m} for m in msgs]
    await supa.insert("messages", rows)
    await supa.update("line_users", {
        "unread": 0, "last_message_at": NOW(),
        "last_message_text": (msgs[-1].get("text") or f"[{msgs[-1].get('type')}]")[:200],
    }, {"line_user_id": f"eq.{uid}"})
    return {"ok": True, "requestId": rid}


@app.get("/api/users/{uid}")
async def user_detail(uid: str, admin=Depends(current_admin)):
    rows = await supa.select("line_users", params={"line_user_id": f"eq.{uid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบผู้ใช้")
    user = rows[0]
    events = await supa.select("webhook_events", params={
        "line_user_id": f"eq.{uid}", "select": "event_type,message_type,text,postback_data,created_at",
        "order": "created_at.desc", "limit": "20",
    })
    follows = await supa.select("follow_history", params={
        "line_user_id": f"eq.{uid}", "select": "action,is_unblocked,event_ts",
        "order": "created_at.desc", "limit": "20",
    })
    # ดึงสด LINE profile + rich menu ปัจจุบัน
    live = {}
    try:
        p = await line.get_profile(uid)
        if p:
            live["profile"] = p
        rid = await line.user_richmenu_get(uid)
        live["richMenuId"] = rid
    except Exception as e:
        live["error"] = str(e)
    regs, slips_rows, tasks_rows, notes_n = [], [], [], 0
    try:
        regs = await supa.select("registrations", params={
            "line_user_id": f"eq.{uid}", "select": "id,course,course_raw,paid,approved,class_id", "limit": "10"})
        slips_rows = await supa.select("slips", params={
            "line_user_id": f"eq.{uid}", "select": "id,status,amount,expected_course,created_at,media_url",
            "order": "created_at.desc", "limit": "6"})
        tasks_rows = await supa.select("tasks", params={
            "line_user_id": f"eq.{uid}", "select": "id,title,status,due_at", "status": "eq.open", "limit": "10"})
        notes_n = await supa.count("contact_notes", {"line_user_id": f"eq.{uid}"})
    except Exception:
        pass
    return {"user": user, "events": events, "follow_history": follows, "live": live,
            "registrations": regs, "slips": slips_rows, "open_tasks": tasks_rows, "notes_count": notes_n,
            "stages": [{"key": s, "label": _STAGE_LABEL.get(s, s)} for s in PIPELINE_STAGES]}


@app.patch("/api/users/{uid}")
async def update_user(uid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    patch = {k: b[k] for k in ("note", "tags", "assigned_to") if k in b}
    if "stage" in b:
        patch["stage"] = b["stage"] or None
        patch["stage_at"] = NOW()
    if "consent" in b:
        patch["consent"] = b["consent"]
        patch["consent_at"] = NOW()
    if "custom" in b:
        cur = await supa.select("line_users", params={"select": "custom", "line_user_id": f"eq.{uid}", "limit": "1"})
        merged = {**((cur[0].get("custom") if cur else {}) or {}), **b["custom"]}
        patch["custom"] = {k: v for k, v in merged.items() if v not in (None, "")}
    patch["updated_at"] = NOW()
    await supa.update("line_users", patch, {"line_user_id": f"eq.{uid}"})
    return {"ok": True}


PIPELINE_STAGES = ["lead", "interested", "registered", "paid", "enrolled", "completed", "alumni", "lost"]
_STAGE_LABEL = {"lead": "Lead", "interested": "สนใจ", "registered": "ลงทะเบียน", "paid": "จ่ายแล้ว",
                "enrolled": "เข้าเรียน", "completed": "เรียนจบ", "alumni": "ศิษย์เก่า", "lost": "หลุด"}


@app.get("/api/pipeline")
async def pipeline(admin=Depends(current_admin)):
    counts = {}
    for s in PIPELINE_STAGES:
        counts[s] = await supa.count("line_users", {"stage": f"eq.{s}", "is_following": "eq.true"})
    counts["_none"] = await supa.count("line_users", {"stage": "is.null", "is_following": "eq.true"})
    return {"stages": [{"key": s, "label": _STAGE_LABEL[s], "count": counts[s]} for s in PIPELINE_STAGES],
            "unstaged": counts["_none"]}


@app.get("/api/users/{uid}/timeline")
async def user_timeline(uid: str, admin=Depends(current_admin)):
    """รวมทุกกิจกรรมของลูกค้าคนนี้ เรียงตามเวลา"""
    ev: list = []

    def add(ts, kind, text, meta=None):
        if ts:
            ev.append({"at": str(ts), "kind": kind, "text": text, "meta": meta or {}})

    try:
        for m in await supa.select("messages", params={
                "select": "direction,by,msg_type,text,created_at", "line_user_id": f"eq.{uid}",
                "order": "created_at.desc", "limit": "40"}):
            who = "ลูกค้า" if m["direction"] == "in" else ({"auto": "บอท", "gemini": "Gemini",
                   "automation": "Automation", "postback": "ปุ่ม", "system": "ระบบ", "broadcast": "Broadcast"}
                   .get(m.get("by"), "แอดมิน"))
            t = (m.get("text") or f"[{m.get('msg_type')}]")[:200]
            add(m["created_at"], "message", f"{who}: {t}", {"dir": m["direction"]})
    except Exception:
        pass
    for tb, fn in (
        ("follow_history", lambda r: ("follow", "ปลดบล็อก/เพิ่มเพื่อน" if r["action"] == "follow" else "บล็อก/ลบเพื่อน")),
        ("slips", lambda r: ("slip", f"สลิป {r.get('status')} {r.get('amount') or ''} {r.get('expected_course') or ''}")),
        ("registrations", lambda r: ("registration", f"ลงทะเบียน {r.get('course_raw') or r.get('course')}"
                                     + (" (จ่ายแล้ว)" if r.get("paid") else ""))),
        ("richmenu_history", lambda r: ("richmenu", f"เปลี่ยนเมนู → {r.get('new_rich_menu_id') or 'ไม่มี'} ({r.get('source')})")),
        ("contact_notes", lambda r: (r.get("kind") or "note", f"{r.get('author') or ''}: {r.get('body')}")),
        ("tasks", lambda r: ("task", f"งาน: {r.get('title')} [{r.get('status')}]")),
    ):
        try:
            for r in await supa.select(tb, params={
                    "select": "*", "line_user_id": f"eq.{uid}", "order": "created_at.desc", "limit": "25"}):
                k, txt = fn(r)
                add(r.get("event_ts") or r.get("created_at"), k, txt)
        except Exception:
            pass
    ev.sort(key=lambda x: x["at"], reverse=True)
    return {"timeline": ev[:120]}


@app.get("/api/users/{uid}/notes")
async def user_notes(uid: str, admin=Depends(current_admin)):
    return {"notes": await supa.select("contact_notes", params={
        "line_user_id": f"eq.{uid}", "select": "*", "order": "created_at.desc", "limit": "100"})}


@app.post("/api/users/{uid}/notes")
async def user_note_add(uid: str, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    body = (b.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "โน้ตว่าง")
    r = await supa.insert("contact_notes", {
        "line_user_id": uid, "body": body, "kind": b.get("kind", "note"),
        "author": admin.get("name") or admin["userId"][:8]})
    return {"ok": True, "note": r[0] if r else None}


@app.delete("/api/users/{uid}/notes/{nid}")
async def user_note_del(uid: str, nid: int, admin=Depends(current_admin)):
    await supa.delete("contact_notes", {"id": f"eq.{nid}"})
    return {"ok": True}


@app.get("/api/users/{uid}/export")
async def user_export(uid: str, admin=Depends(current_admin)):
    """PDPA — ข้อมูลทั้งหมดของลูกค้าคนนี้ (สำหรับคำขอเข้าถึงข้อมูล)"""
    out: dict = {"exported_at": NOW(), "line_user_id": uid}
    for tb in ("line_users", "registrations", "slips", "contact_notes", "tasks", "messages",
               "webhook_events", "follow_history", "richmenu_history", "attendance", "link_clicks"):
        try:
            out[tb] = await supa.select(tb, params={
                "line_user_id": f"eq.{uid}", "select": "*", "limit": "2000"})
        except Exception as e:
            out[tb] = {"error": str(e)[:80]}
    return out


@app.post("/api/users/{uid}/forget")
async def user_forget(uid: str, req: Request, admin=Depends(current_admin)):
    """PDPA — ลบข้อมูลส่วนบุคคล (anonymize) เก็บเฉพาะสถิติรวม"""
    b = await req.json()
    if b.get("confirm") != uid:
        raise HTTPException(400, "ต้องยืนยันด้วย userId")
    await supa.update("line_users", {
        "display_name": None, "picture_url": None, "status_message": None,
        "note": None, "custom": {}, "tags": [], "consent": False, "consent_at": NOW(),
        "stage": "lost", "updated_at": NOW(),
    }, {"line_user_id": f"eq.{uid}"})
    await supa.update("registrations", {"name": None, "tel": None, "email": None, "org": None},
                     {"line_user_id": f"eq.{uid}"})
    for tb in ("contact_notes", "messages"):
        try:
            await supa.delete(tb, {"line_user_id": f"eq.{uid}"})
        except Exception:
            pass
    await supa.log_operation(admin["userId"], "users.forget", {"uid": uid}, None)
    return {"ok": True}


@app.post("/api/users/merge")
async def users_merge(req: Request, admin=Depends(current_admin)):
    """รวมบัญชีซ้ำ: ย้าย registrations/slips/tasks/notes จาก 'from' -> 'into', mark from.merged_into"""
    b = await req.json()
    src, dst = (b.get("from") or "").strip(), (b.get("into") or "").strip()
    if not (src.startswith("U") and dst.startswith("U") and src != dst):
        raise HTTPException(400, "ต้องระบุ from / into เป็น userId คนละอัน")
    moved = {}
    for tb in ("registrations", "slips", "tasks", "contact_notes", "messages", "webhook_events",
               "follow_history", "richmenu_history", "attendance"):
        try:
            r = await supa.update(tb, {"line_user_id": dst}, {"line_user_id": f"eq.{src}"})
            moved[tb] = len(r or [])
        except Exception as e:
            moved[tb] = f"err: {str(e)[:60]}"
    # รวม tags + note
    su = await supa.select("line_users", params={"select": "tags,note", "line_user_id": f"eq.{src}", "limit": "1"})
    du = await supa.select("line_users", params={"select": "tags,note", "line_user_id": f"eq.{dst}", "limit": "1"})
    if su and du:
        tags = sorted(set((su[0].get("tags") or []) + (du[0].get("tags") or [])))
        note = "\n".join(x for x in [du[0].get("note"), su[0].get("note")] if x)
        await supa.update("line_users", {"tags": tags, "note": note or None, "updated_at": NOW()},
                          {"line_user_id": f"eq.{dst}"})
    await supa.update("line_users", {"is_following": False, "merged_into": dst, "updated_at": NOW()},
                      {"line_user_id": f"eq.{src}"})
    await supa.log_operation(admin["userId"], "users.merge", {"from": src, "into": dst}, moved)
    return {"ok": True, "moved": moved}


@app.post("/api/users/import-mapped")
async def users_import_mapped(req: Request, admin=Depends(current_admin)):
    """นำเข้าจาก CSV ที่ map คอลัมน์แล้ว
    rows: [{userId, displayName?, tags?, note?, custom: {...}}]  """
    b = await req.json()
    rows = b.get("rows", [])
    ts = NOW()
    valid, patch = 0, []
    for r in rows:
        uid = str(r.get("userId", "")).strip()
        if not re.fullmatch(r"U[0-9a-f]{32}", uid):
            continue
        valid += 1
        row = {"line_user_id": uid, "source": "import", "updated_at": ts}
        if r.get("displayName"):
            row["display_name"] = _clean_str(r["displayName"])
        if r.get("note"):
            row["note"] = r["note"]
        if r.get("tags"):
            row["tags"] = r["tags"] if isinstance(r["tags"], list) else [t.strip() for t in str(r["tags"]).replace("|", ",").split(",") if t.strip()]
        if r.get("custom"):
            row["custom"] = {k: v for k, v in r["custom"].items() if v not in (None, "")}
        patch.append(row)
    for i in range(0, len(patch), 400):
        await supa.upsert("line_users", patch[i:i + 400], on_conflict="line_user_id")
    await supa.log_operation(admin["userId"], "users.import_mapped",
                             {"submitted": len(rows)}, {"valid": valid})
    return {"ok": True, "submitted": len(rows), "valid": valid}


# ============================================================
# MESSAGING
# ============================================================
def _normalize_messages(raw):
    """รับ string / list[str] / list[obj] -> list[LINE message obj] (max 5)"""
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for m in raw[:5]:
        out.append({"type": "text", "text": m} if isinstance(m, str) else m)
    return out


# ============================================================
# broadcast postback click tracking — รู้ว่าใคร (userId) คลิกปุ่ม postback ที่มาจาก broadcast ไหน
# วิธีทำ: แนบรหัส broadcast ต่อท้าย data ด้วยตัวคั่นที่มองไม่เห็น (\x1f) ตอนส่ง
# แล้วแกะออกตอนรับ postback event กลับมา (ก่อนเข้ากฎ postback_actions/auto_reply เดิม)
# เพื่อไม่ให้กระทบการ match แบบ exact/prefix ของ data เดิมที่แอดมินตั้งไว้
# ============================================================
_BC_TAG_SEP = "\x1f"


def _walk_actions(node, fn):
    """เดินทุก dict/list หา key 'type'=='postback' เรียก fn(node) ให้ (ใช้กับทั้ง template.actions/columns และ flex ที่ซ้อนลึก)"""
    if isinstance(node, dict):
        if node.get("type") == "postback":
            fn(node)
        for v in node.values():
            _walk_actions(v, fn)
    elif isinstance(node, list):
        for v in node:
            _walk_actions(v, fn)


def _has_postback_action(messages: list) -> bool:
    found = []
    _walk_actions(messages, lambda a: found.append(a))
    return bool(found)


def _tag_broadcast_postbacks(messages: list, broadcast_id) -> list:
    """คืนสำเนาข้อความที่แนบรหัส broadcast ไว้ในทุกปุ่ม postback"""
    if not broadcast_id:
        return messages
    msgs = copy.deepcopy(messages)

    def _tag(a):
        d = a.get("data")
        if d and _BC_TAG_SEP not in d:
            a["data"] = f"{d}{_BC_TAG_SEP}bc{broadcast_id}"
    _walk_actions(msgs, _tag)
    return msgs


def _split_postback_data(data: str):
    """แยก postback data จริงออกจากรหัส broadcast ที่แนบท้าย (ถ้ามี) -> (clean_data, broadcast_id|None)"""
    if data and _BC_TAG_SEP in data:
        clean, _, tag = data.rpartition(_BC_TAG_SEP)
        if tag.startswith("bc") and tag[2:].isdigit():
            return clean, int(tag[2:])
    return data, None


async def _broadcast_begin(actor: str | None, kind: str, messages: list, target_count: int | None) -> tuple[int, list]:
    """สร้างแถว broadcasts (status=sending) ก่อนส่งจริง -> คืน (id, ข้อความที่แนบรหัส postback แล้ว)"""
    row = (await supa.insert("broadcasts", {
        "actor": actor, "kind": kind, "target_count": target_count,
        "messages": messages, "status": "sending",
    }))[0]
    bid = row["id"]
    return bid, _tag_broadcast_postbacks(messages, bid)


async def _broadcast_finish(bid: int, *, status: str, line_request_id: str | None = None,
                            error: str | None = None, target_count: int | None = None) -> None:
    patch = {"status": status, "line_request_id": line_request_id, "error": error}
    if target_count is not None:
        patch["target_count"] = target_count
    await supa.update("broadcasts", patch, {"id": f"eq.{bid}"})


@app.post("/api/message/recipients-preview")
async def recipients_preview(req: Request, admin=Depends(current_admin)):
    """นับปลายทาง + ตัวอย่าง 12 คน (รูป+ชื่อ) ก่อนกดส่งจริง"""
    b = await req.json()
    if b.get("mode") == "broadcast":
        cnt = await supa.count("line_users", {"is_following": "eq.true"})
        sample = await supa.select("line_users", params={
            "select": "line_user_id,display_name,picture_url", "is_following": "eq.true",
            "order": "updated_at.desc", "limit": "12",
        })
        return {"count": cnt, "sample": sample, "exact": True}

    if b.get("segmentId"):
        seg = await supa.select("segments", params={"id": f"eq.{b['segmentId']}", "select": "filter", "limit": "1"})
        f = seg[0]["filter"] if seg else {}
    elif b.get("filter"):
        f = b["filter"]
    elif b.get("mode") == "push":
        uids = [u.strip() for u in (b.get("to") or "").replace("\n", ",").split(",") if u.strip()]
        sample = await supa.select("line_users", params={
            "select": "line_user_id,display_name,picture_url",
            "line_user_id": f"in.({','.join(uids[:12])})", "limit": "12",
        }) if uids else []
        return {"count": len(uids), "sample": sample, "exact": True}
    else:
        f = {"tag": b.get("tag"), "menu": b.get("menu")}

    params = _filter_to_params(f)
    uids = await _uids_by_filter(f)
    sample_ids = uids[:12]
    sample = await supa.select("line_users", params={
        "select": "line_user_id,display_name,picture_url",
        "line_user_id": f"in.({','.join(sample_ids)})", "limit": "12",
    }) if sample_ids else []
    return {"count": len(uids), "sample": sample, "exact": True}


# ---------- message templates ----------
@app.get("/api/message-templates")
async def list_msg_templates(admin=Depends(current_admin)):
    return {"templates": await supa.select("message_templates", params={
        "select": "*", "order": "updated_at.desc",
    })}


@app.post("/api/message-templates")
async def save_msg_template(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    row = {"name": b["name"], "messages": _normalize_messages(b.get("messages", [])),
           "created_by": admin["userId"], "updated_at": NOW()}
    if b.get("id"):
        await supa.update("message_templates", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"]}
    r = await supa.insert("message_templates", row)
    return {"ok": True, "template": r[0] if r else None}


@app.delete("/api/message-templates/{tid}")
async def del_msg_template(tid: int, admin=Depends(current_admin)):
    await supa.delete("message_templates", {"id": f"eq.{tid}"})
    return {"ok": True}


@app.get("/api/broadcasts/{bid}")
async def get_broadcast(bid: int, admin=Depends(current_admin)):
    rows = await supa.select("broadcasts", params={"id": f"eq.{bid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบ")
    return rows[0]


@app.get("/api/broadcasts/{bid}/clicks")
async def broadcast_clicks(bid: int, admin=Depends(current_admin)):
    """ใครคลิกปุ่ม postback ใน broadcast นี้บ้าง (userId + ชื่อ + ข้อมูลปุ่มที่กด + เวลา)"""
    try:
        rows = await supa.select_all("postback_clicks", params={
            "select": "line_user_id,data,clicked_at", "broadcast_id": f"eq.{bid}",
            "order": "clicked_at.desc"})
    except Exception as e:
        return {"schema_missing": True, "hint": f"ยังไม่ได้รัน migration 0012_postback_clicks.sql — {e}",
                "clicks": [], "unique_users": 0, "by_data": []}
    uids = list({r["line_user_id"] for r in rows if r.get("line_user_id")})
    names = {}
    for i in range(0, len(uids), 80):
        chunk = uids[i:i + 80]
        try:
            for u in await supa.select("line_users", params={
                    "select": "line_user_id,display_name,picture_url,tags",
                    "line_user_id": f"in.({','.join(chunk)})", "limit": "80"}):
                names[u["line_user_id"]] = u
        except Exception:
            pass
    by_data: dict = {}
    for r in rows:
        by_data.setdefault(r.get("data") or "(ไม่ระบุ)", 0)
        by_data[r.get("data") or "(ไม่ระบุ)"] += 1
    for r in rows:
        u = names.get(r["line_user_id"], {})
        r["display_name"] = u.get("display_name")
        r["picture_url"] = u.get("picture_url")
        r["tags"] = u.get("tags")
    return {
        "total_clicks": len(rows), "unique_users": len(uids),
        "by_data": sorted([{"data": k, "count": v} for k, v in by_data.items()], key=lambda x: -x["count"]),
        "clicks": rows[:500],
    }


@app.post("/api/message/validate")
async def msg_validate(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    ok, txt = await line.validate_messages(_normalize_messages(b.get("messages", [])))
    return {"ok": ok, "detail": txt}


@app.post("/api/message/test")
async def msg_test(req: Request, admin=Depends(current_admin)):
    """ส่งข้อความหาตัวเอง (ผู้ที่ล็อกอินอยู่) เพื่อทดสอบก่อนส่งจริง"""
    b = await req.json()
    msgs = _normalize_messages(b.get("messages", []))
    code, txt, rid = await line.push(admin["userId"], msgs)
    if code != 200:
        raise HTTPException(400, txt)
    return {"ok": True, "requestId": rid, "to": admin["userId"]}


@app.post("/api/message/push")
async def msg_push(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    to = b.get("to")
    nd = bool(b.get("notificationDisabled"))
    msgs = _normalize_messages(b.get("messages", []))
    kind, cnt = ("multicast", len(to)) if isinstance(to, list) and len(to) > 1 else ("push", 1)
    bid, msgs = await _broadcast_begin(admin["userId"], kind, msgs, cnt)
    if isinstance(to, list):
        if len(to) == 1:
            code, txt, rid = await line.push(to[0], msgs, nd)
        else:
            code, txt, rid = await line.multicast(to, msgs, nd)
    else:
        code, txt, rid = await line.push(to, msgs, nd)
    status = "sent" if code == 200 else "failed"
    await _broadcast_finish(bid, status=status, line_request_id=rid,
                            error=None if status == "sent" else txt[:300])
    if code != 200:
        raise HTTPException(400, txt)
    return {"ok": True, "requestId": rid, "sent": cnt}


@app.post("/api/message/bulk")
async def msg_bulk(req: Request, admin=Depends(current_admin)):
    """ส่งหา userId จำนวนมากพร้อมกัน — แบ่ง batch ละ 500 ยิง multicast ขนานกัน
    body: { userIds: [...] | "…\\n…", messages, notificationDisabled }"""
    b = await req.json()
    raw = b.get("userIds", [])
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)

    seen, uids, invalid = set(), [], []
    for u in raw:
        u = str(u).strip()
        if not u:
            continue
        if re.fullmatch(r"U[0-9a-f]{32}", u):
            if u not in seen:
                seen.add(u)
                uids.append(u)
        else:
            invalid.append(u)
    if not uids:
        raise HTTPException(400, "ไม่มี userId ที่ถูกต้อง (ต้องเป็น U + 32 hex)")

    msgs = _normalize_messages(b.get("messages", []))
    nd = bool(b.get("notificationDisabled"))
    chunks = [uids[i:i + 500] for i in range(0, len(uids), 500)]

    bid, msgs = await _broadcast_begin(admin["userId"], "multicast", msgs, len(uids))
    sem = asyncio.Semaphore(5)
    res = {"total": len(uids), "sent": 0, "failed": 0, "batches": len(chunks),
           "ok_batches": 0, "errors": [], "duplicate": len(raw) - len(uids) - len(invalid),
           "invalid": invalid[:30], "invalid_count": len(invalid)}
    last_rid = [None]

    async def one(idx, ch):
        async with sem:
            code, txt, rid = await line.multicast(ch, msgs, nd)
            if rid:
                last_rid[0] = rid
            if code == 200:
                res["sent"] += len(ch)
                res["ok_batches"] += 1
            else:
                res["failed"] += len(ch)
                if len(res["errors"]) < 20:
                    res["errors"].append({"batch": idx, "n": len(ch), "code": code, "msg": txt[:150]})

    await asyncio.gather(*[one(i, c) for i, c in enumerate(chunks)])

    status = "sent" if res["failed"] == 0 else ("partial" if res["sent"] else "failed")
    await _broadcast_finish(bid, status="sent" if status == "sent" else "failed",
                            line_request_id=last_rid[0],
                            error=None if status == "sent" else f"{res['failed']} ล้มเหลว")
    await supa.log_operation(admin["userId"], "message.bulk",
                             {"total": len(uids), "batches": len(chunks)}, res, status)
    return res


@app.post("/api/message/broadcast")
async def msg_broadcast(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    msgs = _normalize_messages(b.get("messages", []))
    following = await supa.count("line_users", {"is_following": "eq.true"})
    bid, msgs = await _broadcast_begin(admin["userId"], "broadcast", msgs, following)
    code, txt, rid = await line.broadcast(msgs)
    status = "sent" if code == 200 else "failed"
    await _broadcast_finish(bid, status=status, line_request_id=rid,
                            error=None if status == "sent" else txt[:300])
    if code != 200:
        raise HTTPException(400, txt)
    return {"ok": True, "requestId": rid}


@app.post("/api/message/multicast-from-db")
async def msg_multicast_db(req: Request, admin=Depends(current_admin)):
    """ส่งหา user ใน DB ตาม filter / segmentId / tag+menu"""
    b = await req.json()
    if b.get("segmentId"):
        seg = await supa.select("segments", params={"id": f"eq.{b['segmentId']}", "select": "filter", "limit": "1"})
        f = seg[0]["filter"] if seg else {}
    elif b.get("filter"):
        f = b["filter"]
    else:
        f = {"tag": b.get("tag"), "menu": b.get("menu")}
    uids = await _uids_by_filter(f)
    if not uids:
        raise HTTPException(400, "ไม่มีปลายทาง")
    msgs = _normalize_messages(b.get("messages", []))
    bid, msgs = await _broadcast_begin(admin["userId"], "multicast", msgs, len(uids))
    last_rid, sent, failed = None, 0, 0
    for i in range(0, len(uids), 500):
        code, txt, rid = await line.multicast(uids[i:i + 500], msgs)
        last_rid = rid
        if code == 200:
            sent += len(uids[i:i + 500])
        else:
            failed += len(uids[i:i + 500])
    await _broadcast_finish(bid, status="sent" if failed == 0 else "failed", line_request_id=last_rid)
    return {"ok": failed == 0, "target": len(uids), "sent": sent, "failed": failed}


@app.get("/api/broadcasts")
async def list_broadcasts(admin=Depends(current_admin), limit: int = 50):
    return {"broadcasts": await supa.select("broadcasts", params={
        "select": "*", "order": "created_at.desc", "limit": str(limit),
    })}


# ============================================================
# STATS
# ============================================================
@app.get("/api/stats/quota")
async def stats_quota(admin=Depends(current_admin)):
    return await line.message_quota()


@app.get("/api/stats/insight")
async def stats_insight(admin=Depends(current_admin), date: str = ""):
    if not date:
        date = (dt.date.today() - dt.timedelta(days=2)).strftime("%Y%m%d")
    followers = await line.insight_followers(date)
    demo = await line.insight_demographic()
    delivery = await line.insight_message_delivery(date)
    # snapshot
    try:
        await supa.upsert("stats_daily", {
            "day": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "followers": followers.get("followers"),
            "targeted_reaches": followers.get("targetedReaches"),
            "blocks": followers.get("blocks"),
            "raw": {"followers": followers, "delivery": delivery},
            "captured_at": NOW(),
        }, on_conflict="day")
    except Exception:
        pass
    return {"date": date, "followers": followers, "demographic": demo, "delivery": delivery}


@app.get("/api/stats/funnel")
async def stats_funnel(admin=Depends(current_admin), tag: str = ""):
    total = await supa.count("line_users")
    following = await supa.count("line_users", {"is_following": "eq.true"})
    messaged = await supa.count("line_users", {"is_following": "eq.true", "last_message_at": "not.is.null"})
    has_menu = await supa.count("line_users", {"is_following": "eq.true", "current_rich_menu_id": "not.is.null"})
    tagged = await supa.count("line_users", {"is_following": "eq.true", "tags": "cs.{" + tag + "}"}) if tag else None

    # cohort: follow แต่ละสัปดาห์ (8 สัปดาห์) -> ยัง follow กี่ %
    fh = await supa.select_all("follow_history", params={"select": "line_user_id,action,event_ts"})
    first_follow: dict[str, str] = {}
    for r in sorted(fh, key=lambda x: x.get("event_ts") or ""):
        if r["action"] == "follow" and r["line_user_id"] not in first_follow:
            first_follow[r["line_user_id"]] = (r.get("event_ts") or "")[:10]
    still = {u["line_user_id"] for u in await supa.select_all("line_users", params={
        "select": "line_user_id", "is_following": "eq.true"})}
    cohorts: dict[str, dict] = {}
    for uid, d in first_follow.items():
        if not d:
            continue
        wk = d[:7]  # เดือน
        c = cohorts.setdefault(wk, {"month": wk, "joined": 0, "retained": 0})
        c["joined"] += 1
        if uid in still:
            c["retained"] += 1
    cohort_list = sorted(cohorts.values(), key=lambda x: x["month"])[-8:]
    for c in cohort_list:
        c["rate"] = round(c["retained"] / c["joined"] * 100) if c["joined"] else 0

    return {
        "funnel": [
            {"step": "ผู้ใช้ในระบบ", "count": total},
            {"step": "กำลังติดตาม", "count": following},
            {"step": "เคยทักเข้ามา", "count": messaged},
            {"step": "มี Rich Menu", "count": has_menu},
            *([{"step": f"tag: {tag}", "count": tagged}] if tag else []),
        ],
        "cohorts": cohort_list,
    }


@app.get("/api/stats/history")
async def stats_history(admin=Depends(current_admin), days: int = 30):
    return {"days": await supa.select("stats_daily", params={
        "select": "*", "order": "day.desc", "limit": str(days),
    })}


@app.get("/api/stats/follows")
async def stats_follows(admin=Depends(current_admin), days: int = 14):
    """สรุป follow/unfollow จาก webhook (real-time) รายวัน"""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    rows = await supa.select_all("follow_history", params={
        "select": "action,is_unblocked,event_ts,created_at",
    })
    by_day: dict[str, dict] = {}
    unblocked = newfollow = unfollow = 0
    for r in rows:
        d = (r.get("event_ts") or r["created_at"])[:10]
        b = by_day.setdefault(d, {"day": d, "follow": 0, "unfollow": 0, "unblock": 0})
        if r["action"] == "follow":
            b["follow"] += 1
            if r.get("is_unblocked"):
                b["unblock"] += 1
                unblocked += 1
            else:
                newfollow += 1
        else:
            b["unfollow"] += 1
            unfollow += 1
    series = sorted(by_day.values(), key=lambda x: x["day"])[-days:]
    for s in series:
        s["net"] = s["follow"] - s["unfollow"]
    return {"series": series,
            "totals": {"new_follow": newfollow, "unblock": unblocked, "unfollow": unfollow,
                       "net": newfollow + unblocked - unfollow}}


# ============================================================
# EVENTS / LOGS
# ============================================================
@app.get("/api/events")
async def events(admin=Depends(current_admin), limit: int = 100, type: str = "", uid: str = ""):
    params = {"select": "id,event_type,line_user_id,message_type,text,reply_token,postback_data,auto_replied,created_at",
              "order": "created_at.desc", "limit": str(min(limit, 500))}
    if type:
        params["event_type"] = f"eq.{type}"
    if uid:
        params["line_user_id"] = f"eq.{uid}"
    return {"events": await supa.select("webhook_events", params=params)}


@app.post("/api/events/{eid}/reply")
async def reply_to_event(eid: int, req: Request, admin=Depends(current_admin)):
    """ตอบกลับด้วย replyToken ของ event นั้น (ใช้ได้ ~1 นาทีหลัง event เข้ามา)"""
    b = await req.json()
    rows = await supa.select("webhook_events", params={
        "id": f"eq.{eid}", "select": "reply_token,line_user_id,created_at", "limit": "1"})
    if not rows or not rows[0].get("reply_token"):
        raise HTTPException(400, "event นี้ไม่มี reply token")
    ev = rows[0]
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ev["created_at"])).total_seconds()
    msgs = _normalize_messages(b.get("messages", []))
    code, txt = await line.reply(ev["reply_token"], msgs)
    if code != 200:
        # reply token หมดอายุ -> fallback เป็น push
        if ev.get("line_user_id"):
            pc, ptxt, prid = await line.push(ev["line_user_id"], msgs)
            if pc == 200:
                await supa.update("webhook_events", {"auto_replied": True}, {"id": f"eq.{eid}"})
                return {"ok": True, "via": "push", "note": f"reply token ใช้ไม่ได้ (อายุ {int(age)}s) เลยส่ง push แทน"}
        raise HTTPException(400, f"reply ไม่สำเร็จ: {txt[:150]}")
    await supa.update("webhook_events", {"auto_replied": True}, {"id": f"eq.{eid}"})
    return {"ok": True, "via": "reply"}


@app.get("/api/operations")
async def operations(admin=Depends(current_admin), limit: int = 100):
    return {"operations": await supa.select("operations", params={
        "select": "*", "order": "created_at.desc", "limit": str(min(limit, 500)),
    })}


# ============================================================
# AUTO-REPLY (keyword responder)
# ============================================================
@app.get("/api/auto-replies")
async def list_auto_replies(admin=Depends(current_admin)):
    return {"rules": await supa.select("auto_replies", params={
        "select": "*", "order": "priority.desc,id.asc",
    })}


@app.post("/api/auto-replies")
async def create_auto_reply(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    row = {
        "name": b.get("name"),
        "enabled": b.get("enabled", True),
        "trigger": b.get("trigger", "text"),
        "match_type": b.get("match_type", "contains"),
        "keywords": b.get("keywords", []),
        "messages": _normalize_messages(b.get("messages", [])),
        "priority": int(b.get("priority", 0)),
        "created_by": admin["userId"],
    }
    if b.get("id"):
        await supa.update("auto_replies", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"]}
    r = await supa.insert("auto_replies", row)
    return {"ok": True, "rule": r[0] if r else None}


# ---------- postback actions ----------
@app.get("/api/postbacks")
async def list_postbacks(admin=Depends(current_admin)):
    return {"actions": await supa.select("postback_actions", params={
        "select": "*", "order": "data"})}


@app.post("/api/postbacks")
async def save_postback(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    data = (b.get("data") or "").strip()
    if not data:
        raise HTTPException(400, "ต้องระบุ postback data")
    row = {
        "data": data, "label": b.get("label"), "enabled": b.get("enabled", True),
        "match": b.get("match", "exact"),
        "messages": _normalize_messages(b.get("messages", [])),
        "note": b.get("note"), "created_by": admin["userId"], "updated_at": NOW(),
    }
    if b.get("id"):
        await supa.update("postback_actions", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"]}
    await supa.upsert("postback_actions", row, on_conflict="data")
    return {"ok": True}


@app.post("/api/postbacks/import")
async def import_postbacks(req: Request, admin=Depends(current_admin)):
    """รับ list ของ postback code -> สร้าง stub (ปิดไว้) ถ้ายังไม่มี"""
    b = await req.json()
    codes = []
    for c in b.get("codes", []):
        c = str(c).strip()
        if c:
            codes.append(c)
    codes = list(dict.fromkeys(codes))
    if not codes:
        raise HTTPException(400, "ไม่มี code")
    existing = {r["data"] for r in await supa.select("postback_actions", params={
        "select": "data", "data": f"in.({','.join(codes)})", "limit": "1000"})}
    new = [{"data": c, "label": c, "enabled": False, "match": "exact",
            "messages": [{"type": "text", "text": f"[ตั้งข้อความตอบสำหรับ {c}]"}],
            "created_by": admin["userId"]} for c in codes if c not in existing]
    if new:
        for i in range(0, len(new), 100):
            await supa.insert("postback_actions", new[i:i + 100])
    return {"ok": True, "total": len(codes), "created": len(new), "existing": len(existing)}


@app.delete("/api/postbacks/{pid}")
async def delete_postback(pid: int, admin=Depends(current_admin)):
    await supa.delete("postback_actions", {"id": f"eq.{pid}"})
    return {"ok": True}


@app.delete("/api/auto-replies/{rid}")
async def delete_auto_reply(rid: int, admin=Depends(current_admin)):
    await supa.delete("auto_replies", {"id": f"eq.{rid}"})
    return {"ok": True}


# ============================================================
# CRON: snapshot สถิติรายวัน + รัน scheduled jobs
# ============================================================
def _check_cron_key(request: Request):
    ip = request.headers.get("x-forwarded-for", "?").split(",")[0].strip()
    _rate_limit(f"cron:{ip}", limit=30, window=60)
    if CRON_SECRET:
        key = request.query_params.get("key") or request.headers.get("x-cron-key", "")
        if key != CRON_SECRET:
            raise HTTPException(403, "bad cron key")


BACKUP_TABLES = ("admins", "line_users", "auto_replies", "segments", "message_templates",
                 "rich_menus", "scheduled_jobs", "registrations", "slips", "payment_accounts",
                 "postback_actions", "automations", "app_settings", "liff_apps",
                 "tasks", "classes", "attendance", "contact_notes", "kb_articles",
                 "richmenu_history", "follow_history")


async def _make_backup():
    day = dt.date.today().isoformat()
    dump = {}
    for tb in BACKUP_TABLES:
        try:
            dump[tb] = await supa.select_all(tb, params={"select": "*"})
        except Exception as e:
            dump[tb] = {"error": str(e)}
    raw = json.dumps(dump, ensure_ascii=False).encode()
    size_kb = len(raw) // 1024
    blob = gzip.compress(raw, 6)
    # เก็บสำเนาไว้ที่ Storage ด้วย (แยกจาก DB — DB ล่มก็ยังกู้ได้)
    ext_url = None
    storage_err = None
    try:
        await supa.storage_ensure_bucket("db-backups", public=False)
        await supa.storage_upload("db-backups", f"{day}.json.gz", blob, "application/gzip")
        ext_url = await supa.storage_sign_url("db-backups", f"{day}.json.gz", 7 * 86400)
    except Exception as e:
        storage_err = str(e)
        print("backup storage upload error:", e)
    await supa.upsert("backups", {"day": day, "tables": dump, "size_kb": size_kb},
                      on_conflict="day")
    old = await supa.select("backups", params={"select": "id,day", "order": "day.desc", "limit": "60"})
    for r in old[30:]:
        await supa.delete("backups", {"id": f"eq.{r['id']}"})
        try:
            await supa.storage_delete("db-backups", f"{r['day']}.json.gz")
        except Exception:
            pass
    return {"day": day, "size_kb": size_kb, "gz_kb": len(blob) // 1024,
            "storage": bool(ext_url), "storage_url": ext_url, "storage_err": storage_err,
            "rows": {k: (len(v) if isinstance(v, list) else 0) for k, v in dump.items()}}


async def _retag_from_registrations() -> dict:
    """อ่าน registrations ทั้งหมด -> tag line_users (คอร์ส + ลงทะเบียน + จ่ายแล้ว)"""
    regs = await supa.select_all("registrations", params={"select": "line_user_id,course,paid"})
    by_uid: dict[str, dict] = {}
    for r in regs:
        if not r.get("line_user_id"):
            continue
        u = by_uid.setdefault(r["line_user_id"], {"courses": set(), "paid": False})
        if r.get("course"):
            u["courses"].add(r["course"])
        if r.get("paid"):
            u["paid"] = True
    uids = list(by_uid)
    touched = 0
    for i in range(0, len(uids), 80):
        chunk = uids[i:i + 80]
        try:
            existing = {x["line_user_id"]: x for x in await supa.select("line_users", params={
                "select": "line_user_id,tags", "line_user_id": f"in.({','.join(chunk)})", "limit": "200"})}
            patch = []
            for uid in chunk:
                info = by_uid[uid]
                cur = set((existing.get(uid) or {}).get("tags") or [])
                new = set(cur) | {"ลงทะเบียน"} | info["courses"]
                if info["paid"]:
                    new.add("จ่ายแล้ว")
                row = {"line_user_id": uid, "tags": sorted(new), "updated_at": NOW()}
                if uid not in existing:
                    row["source"] = "registration"
                    row["is_following"] = True
                if new != cur or uid not in existing:
                    patch.append(row)
            if patch:
                await supa.upsert("line_users", patch, on_conflict="line_user_id")
                touched += len(patch)
        except Exception as e:
            print("retag chunk error:", e)
    return {"registrations": len(regs), "people": len(uids), "users_tagged": touched}


@app.api_route("/api/cron/registrations-retag", methods=["GET", "POST"])
async def cron_registrations_retag(request: Request):
    _check_cron_key(request)
    res = await _retag_from_registrations()
    await supa.log_operation("cron", "registrations.retag", None, res)
    return {"ok": True, **res}


@app.post("/api/registrations/retag")
async def registrations_retag(admin=Depends(current_admin)):
    res = await _retag_from_registrations()
    await supa.log_operation(admin["userId"], "registrations.retag", None, res)
    return {"ok": True, **res}


@app.api_route("/api/cron/backup", methods=["GET", "POST"])
async def cron_backup(request: Request):
    _check_cron_key(request)
    try:
        res = await _make_backup()
        await supa.log_operation("cron", "backup", None, res)
        return {"ok": True, **res}
    except Exception as e:
        await alert_admin("Backup ล้มเหลว", str(e), "backup")
        raise HTTPException(500, str(e))


@app.post("/api/backup/now")
async def backup_now(admin=Depends(current_admin)):
    res = await _make_backup()
    await supa.log_operation(admin["userId"], "backup.manual", None, res)
    return {"ok": True, **res}


@app.get("/api/backup/list")
async def backup_list(admin=Depends(current_admin)):
    rows = await supa.select("backups", params={
        "select": "id,day,size_kb,created_at", "order": "day.desc", "limit": "30"})
    for r in rows:
        try:
            r["storage_url"] = await supa.storage_sign_url(
                "db-backups", f"{r['day']}.json.gz", 7 * 86400)
        except Exception:
            r["storage_url"] = None
    return {"backups": rows}


@app.get("/api/backup/{bid}")
async def backup_get(bid: int, admin=Depends(current_admin)):
    rows = await supa.select("backups", params={"id": f"eq.{bid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบ")
    return rows[0]


@app.api_route("/api/cron/snapshot-stats", methods=["GET", "POST"])
async def cron_snapshot_stats(request: Request):
    _check_cron_key(request)
    date = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    try:
        followers = await line.insight_followers(date)
        delivery = await line.insight_message_delivery(date)
        quota = await line.message_quota()
    except Exception as e:
        raise HTTPException(400, f"insight error: {e}")

    q = quota.get("quota", {}).get("quota") or quota.get("quota", {})
    await supa.upsert("stats_daily", {
        "day": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "followers": followers.get("followers"),
        "targeted_reaches": followers.get("targetedReaches"),
        "blocks": followers.get("blocks"),
        "quota_type": q.get("type") if isinstance(q, dict) else None,
        "quota_limit": q.get("value") if isinstance(q, dict) else None,
        "quota_used": quota.get("totalUsage"),
        "raw": {"followers": followers, "delivery": delivery},
        "captured_at": NOW(),
    }, on_conflict="day")
    await supa.log_operation("cron", "stats.snapshot", {"date": date}, followers)
    return {"ok": True, "date": date, "followers": followers.get("followers")}


async def _cron_last_run(action_like: str) -> str | None:
    rows = await supa.select("operations", params={
        "select": "created_at", "action": f"like.{action_like}",
        "order": "created_at.desc", "limit": "1"})
    return rows[0]["created_at"] if rows else None


@app.api_route("/api/cron/heartbeat", methods=["GET", "POST"])
async def cron_heartbeat(request: Request):
    """เช็คว่า cron จำเป็นยังทำงานอยู่ไหม — แจ้งแอดมินถ้าเงียบเกินกำหนด"""
    _check_cron_key(request)
    checks = {
        "richmenu.sync": 2 * 3600,
        "automation.run": 2 * 3600,
        "stats.snapshot": 30 * 3600,
        "backup": 30 * 3600,
    }
    stale = []
    for act, limit in checks.items():
        last = await _cron_last_run(f"*{act}*")
        if not last:
            stale.append(f"{act}: ไม่เคยรัน")
            continue
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds()
        if age > limit:
            stale.append(f"{act}: {int(age // 3600)} ชม.ที่แล้ว")
    if stale:
        await alert_admin("⚠️ Cron ไม่ทำงาน", "\n".join(stale), throttle_key="cron_stale")
    await supa.log_operation("cron", "heartbeat", None, {"stale": stale})
    return {"ok": not stale, "stale": stale}


@app.api_route("/api/cron/daily-digest", methods=["GET", "POST"])
async def cron_daily_digest(request: Request):
    """สรุปประจำวันส่งเข้า LINE แอดมิน"""
    _check_cron_key(request)
    y0 = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    t0 = dt.date.today().isoformat()

    async def _cnt(tb, extra=None):
        p = {"and": f"(created_at.gte.{y0},created_at.lt.{t0})"}
        if extra:
            p.update(extra)
        try:
            return await supa.count(tb, p)
        except Exception:
            return 0

    new_follow = await _cnt("follow_history", {"action": "eq.follow"})
    unfollow = await _cnt("follow_history", {"action": "eq.unfollow"})
    new_reg = await _cnt("registrations")
    msg_in = await _cnt("messages", {"direction": "eq.in"})
    slip_new = await supa.count("slips", {"status": "eq.new"})
    slip_review = await supa.count("slips", {"status": "eq.review"})
    tasks_overdue = 0
    try:
        tasks_overdue = await supa.count("tasks", {"status": "eq.open", "due_at": f"lt.{NOW()}"})
    except Exception:
        pass
    # รายได้เมื่อวาน
    rev = 0.0
    try:
        for s in await supa.select("slips", params={
                "select": "amount,reviewed_at,created_at", "status": "eq.verified",
                "and": f"(reviewed_at.gte.{y0},reviewed_at.lt.{t0})", "limit": "500"}):
            rev += float(s.get("amount") or 0)
    except Exception:
        pass

    lines = [f"📊 สรุปวันที่ {y0}",
             f"👥 เพิ่มเพื่อน {new_follow} · เลิกติดตาม {unfollow}",
             f"📝 สมัครใหม่ {new_reg}",
             f"💬 ข้อความเข้า {msg_in}",
             f"🧾 สลิปรอตรวจ {slip_new + slip_review} · ยืนยันแล้ว ฿{round(rev):,}"]
    if tasks_overdue:
        lines.append(f"⏰ งานเกินกำหนด {tasks_overdue}")
    lines.append(f"\nเปิดดู: {APP_URL}")
    text = "\n".join(lines)
    sent = 0
    for uid in ALERT_USER_IDS:
        try:
            code, *_ = await line.push(uid, [{"type": "text", "text": text}])
            sent += (code == 200)
        except Exception:
            pass
    await supa.log_operation("cron", "daily.digest", None, {"sent": sent})
    return {"ok": True, "sent": sent, "preview": text}


async def _enroll_inactive_automations():
    """automation trigger=inactive — หา user ที่ไม่ทักมานานตาม trigger_config.days แล้ว enroll
    (dedup ด้วย unique constraint automation_runs) — เรียกจาก cron run-automations"""
    autos = await supa.select("automations", params={
        "select": "id,trigger_config", "enabled": "eq.true", "trigger": "eq.inactive"})
    for a in autos:
        cfg = a.get("trigger_config") or {}
        try:
            days = max(1, int(cfg.get("days", 14)))
        except (TypeError, ValueError):
            days = 14
        cutoff = _iso_ago(days=days)
        floor = _iso_ago(days=days + 30)  # ไม่ backfill คนที่หายไปนานมาก
        rows = await supa.select("line_users", params={
            "select": "line_user_id", "is_following": "eq.true",
            "and": f"(last_message_at.lt.{cutoff},last_message_at.gt.{floor})",
            "limit": "400"})
        uids = [r["line_user_id"] for r in rows]
        if uids:
            await _enroll_automations("inactive", uids)


@app.api_route("/api/cron/run-automations", methods=["GET", "POST"])
async def cron_run_automations(request: Request):
    _check_cron_key(request)
    try:
        await _run_webhook_jobs(limit=10)  # backup drain (เผื่อ fire-and-forget ไม่ถึง)
    except Exception as e:
        print("webhook jobs drain error:", e)
    try:
        await _enroll_inactive_automations()
    except Exception as e:
        print("inactive enroll error:", e)
    due = await supa.select("automation_runs", params={
        "select": "*", "status": "eq.pending", "run_at": f"lte.{NOW()}",
        "order": "run_at.asc", "limit": "200"})
    if not due:
        await supa.log_operation("cron", "automation.run", None, {"ran": 0})  # heartbeat
        return {"ok": True, "ran": 0}
    autos = {a["id"]: a for a in await supa.select("automations", params={"select": "*"})}
    sent = failed = 0
    for run in due:
        a = autos.get(run["automation_id"])
        if not a or not a.get("enabled"):
            await supa.update("automation_runs", {"status": "skipped"}, {"id": f"eq.{run['id']}"})
            continue
        steps = a.get("steps") or []
        idx = run["step_idx"]
        if idx >= len(steps):
            await supa.update("automation_runs", {"status": "done"}, {"id": f"eq.{run['id']}"})
            continue
        # ยังตามอยู่ไหม
        u = await supa.select("line_users", params={
            "select": "is_following", "line_user_id": f"eq.{run['line_user_id']}", "limit": "1"})
        if not u or not u[0].get("is_following"):
            await supa.update("automation_runs", {"status": "skipped"}, {"id": f"eq.{run['id']}"})
            continue
        msgs = _normalize_messages(steps[idx].get("messages", []))
        code, _, _ = await line.push(run["line_user_id"], msgs)
        ok = code == 200
        sent += ok
        failed += (not ok)
        try:
            await supa.insert("messages", [{"line_user_id": run["line_user_id"], "direction": "out",
                                            "by": "automation", "msg_type": m.get("type"),
                                            "text": m.get("text"), "payload": m} for m in msgs])
        except Exception:
            pass
        await supa.update("automation_runs", {"status": "done" if ok else "failed"}, {"id": f"eq.{run['id']}"})
        # step ถัดไป
        if ok and idx + 1 < len(steps):
            nxt = steps[idx + 1]
            delay = int(nxt.get("delayHours", 0)) * 3600 + int(nxt.get("delayMinutes", 0)) * 60
            run_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(delay, 60))).isoformat()
            try:
                await supa.insert("automation_runs", {
                    "automation_id": a["id"], "line_user_id": run["line_user_id"],
                    "step_idx": idx + 1, "run_at": run_at, "status": "pending"})
            except Exception:
                pass
        if ok and idx == 0:
            await supa.update("automations", {"runs": (a.get("runs") or 0) + 1}, {"id": f"eq.{a['id']}"})
    await supa.log_operation("cron", "automation.run", {"due": len(due)}, {"sent": sent, "failed": failed})
    return {"ok": True, "ran": len(due), "sent": sent, "failed": failed}


@app.get("/api/automations")
async def list_automations(admin=Depends(current_admin)):
    return {"automations": await supa.select("automations", params={"select": "*", "order": "id.desc"})}


@app.post("/api/automations")
async def save_automation(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    steps = []
    for s in b.get("steps", []):
        steps.append({
            "delayHours": int(s.get("delayHours", 0)),
            "delayMinutes": int(s.get("delayMinutes", 0)),
            "messages": _normalize_messages(s.get("messages", [])),
        })
    row = {"name": b["name"], "enabled": b.get("enabled", True),
           "trigger": b.get("trigger", "follow"), "trigger_config": b.get("triggerConfig", {}),
           "steps": steps, "created_by": admin["userId"]}
    if b.get("id"):
        await supa.update("automations", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"]}
    r = await supa.insert("automations", row)
    return {"ok": True, "automation": r[0] if r else None}


@app.delete("/api/automations/{aid}")
async def del_automation(aid: int, admin=Depends(current_admin)):
    await supa.delete("automations", {"id": f"eq.{aid}"})
    await supa.delete("automation_runs", {"automation_id": f"eq.{aid}", "status": "eq.pending"})
    return {"ok": True}


@app.api_route("/api/cron/run-scheduled", methods=["GET", "POST"])
async def cron_run_scheduled(request: Request):
    _check_cron_key(request)
    due = await supa.select("scheduled_jobs", params={
        "select": "*", "status": "eq.pending", "run_at": f"lte.{NOW()}",
        "order": "run_at.asc", "limit": "10",
    })
    ran = []
    for job in due:
        p = job["payload"] or {}
        try:
            kind = job["kind"]
            if kind == "broadcast":
                sched_msgs = _normalize_messages(p.get("messages", []))
                sbid, sched_msgs = await _broadcast_begin(job.get("created_by"), "broadcast", sched_msgs, None)
                code, txt, rid = await line.broadcast(sched_msgs)
                ok = code == 200
                await _broadcast_finish(sbid, status="sent" if ok else "failed", line_request_id=rid,
                                        error=None if ok else txt[:300])
                res = {"code": code, "requestId": rid}
            elif kind == "richmenu_default":
                ok, txt = await line.richmenu_set_default(p["richMenuId"])
                res = {"ok": ok, "resp": txt[:150]}
            else:
                ok, res = False, {"error": "unknown kind"}
            await supa.update("scheduled_jobs",
                              {"status": "done" if ok else "failed", "result": res},
                              {"id": f"eq.{job['id']}"})
            ran.append({"id": job["id"], "ok": ok})

            # recurring -> ตั้ง job รอบถัดไป
            rep = job.get("repeat")
            if ok and rep:
                delta = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}.get(rep)
                if delta:
                    nxt = dt.datetime.fromisoformat(job["run_at"]) + dt.timedelta(days=delta)
                    await supa.insert("scheduled_jobs", {
                        "kind": kind, "run_at": nxt.isoformat(), "payload": p,
                        "repeat": rep, "label": job.get("label"),
                        "created_by": job.get("created_by"),
                    })
        except Exception as e:
            await supa.update("scheduled_jobs", {"status": "failed", "result": {"error": str(e)}},
                              {"id": f"eq.{job['id']}"})
    return {"ok": True, "ran": ran}


@app.get("/api/scheduled")
async def list_scheduled(admin=Depends(current_admin)):
    return {"jobs": await supa.select("scheduled_jobs", params={
        "select": "*", "order": "run_at.desc", "limit": "50",
    })}


@app.post("/api/scheduled")
async def create_scheduled(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    kind = b.get("kind", "broadcast")
    payload = {}
    if kind == "broadcast":
        payload = {"messages": _normalize_messages(b.get("messages", []))}
    elif kind == "richmenu_default":
        payload = {"richMenuId": b["richMenuId"]}
    r = await supa.insert("scheduled_jobs", {
        "kind": kind, "run_at": b["runAt"], "payload": payload,
        "repeat": b.get("repeat"), "label": b.get("label"),
        "created_by": admin["userId"],
    })
    return {"ok": True, "job": r[0] if r else None}


@app.delete("/api/scheduled/{jid}")
async def cancel_scheduled(jid: int, admin=Depends(current_admin)):
    await supa.update("scheduled_jobs", {"status": "cancelled"}, {"id": f"eq.{jid}", "status": "eq.pending"})
    return {"ok": True}


# ============================================================
# SEGMENTS (บันทึก filter เป็นกลุ่ม)
# ============================================================
def _filter_to_params(f: dict) -> dict:
    p = {"select": "line_user_id"}
    following = f.get("following")
    if following in ("true", "false", True, False):
        p["is_following"] = f"eq.{str(following).lower()}"
    else:
        p["is_following"] = "eq.true"
    if f.get("menu") == "none" or f.get("noMenu"):
        p["current_rich_menu_id"] = "is.null"
    elif f.get("menu"):
        p["current_rich_menu_id"] = f"eq.{f['menu']}"
    if f.get("source"):
        p["source"] = f"eq.{f['source']}"
    tags = f.get("tags") or ([f["tag"]] if f.get("tag") else [])
    if tags:
        p["tags"] = "cs.{" + ",".join(tags) + "}"
    if f.get("search"):
        s = f["search"]
        p["or"] = f"(line_user_id.ilike.*{s}*,display_name.ilike.*{s}*,note.ilike.*{s}*)"
    if f.get("stage"):
        p["stage"] = f"eq.{f['stage']}"
    # PDPA: ไม่ส่งหาคนที่ขอหยุดข่าวสาร (consent=false) เว้นแต่ระบุ includeOptOut
    if not f.get("includeOptOut"):
        p["consent"] = "not.is.false"
    return p


async def _uids_by_filter(f: dict) -> list[str]:
    rows = await supa.select_all("line_users", params=_filter_to_params(f))
    return [r["line_user_id"] for r in rows]


_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
        "video/mp4": "mp4", "audio/mp4": "m4a", "audio/x-m4a": "m4a"}


@app.post("/api/upload")
async def upload_media(req: Request, admin=Depends(current_admin)):
    """รับ base64 (dataURL) -> เก็บ Supabase Storage -> คืน public URL"""
    b = await req.json()
    data = b.get("dataUrl") or b.get("base64") or ""
    ctype = "image/png"
    if data.startswith("data:"):
        head, data = data.split(",", 1)
        ctype = head[5:].split(";")[0] or ctype
    if ctype not in _EXT:
        raise HTTPException(400, f"ไม่รองรับไฟล์ชนิด {ctype}")
    raw = base64.b64decode(data)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "ไฟล์ใหญ่เกิน 10MB")
    name = f"{dt.date.today().isoformat()}/{int(time.time()*1000)}-{os.urandom(3).hex()}.{_EXT[ctype]}"
    url = await supa.storage_upload("media", name, raw, ctype)
    await supa.log_operation(admin["userId"], "upload", {"type": ctype, "kb": len(raw) // 1024}, {"url": url})
    return {"ok": True, "url": url, "path": name, "size": len(raw)}


@app.get("/api/media")
async def list_media(admin=Depends(current_admin)):
    try:
        items = await supa.storage_list("media")
    except Exception:
        return {"items": []}
    base = f"{SUPABASE_URL}/storage/v1/object/public/media/"
    out = []
    for it in items:
        if it.get("name") and not it["name"].endswith("/"):
            out.append({"name": it["name"], "url": base + it["name"],
                        "size": (it.get("metadata") or {}).get("size"),
                        "created": it.get("created_at")})
    return {"items": out}


# ============================================================
# LIFF
# ============================================================
def _liff_meta(liff_id: str) -> dict:
    """ข้อมูลที่คำนวณได้จาก LIFF ID เอง"""
    channel = liff_id.split("-")[0] if "-" in liff_id else ""
    return {
        "liffId": liff_id,
        "channelId": channel,
        "permanentLink": f"https://liff.line.me/{liff_id}",
        "shortLink": f"line://app/{liff_id}",
        "consoleUrl": f"https://developers.line.biz/console/channel/{channel}/liff" if channel else None,
    }


_liff_token_cache: dict = {"token": None, "exp": 0.0}


async def _liff_channel_token() -> tuple[str | None, str | None]:
    """channel access token สำหรับเรียก LIFF API — คืน (token, error)
    ลำดับ: LINE_LOGIN_CHANNEL_TOKEN ตรง ๆ > JWT assertion (Login channel) > client_credentials (Messaging API)"""
    if LINE_LOGIN_CHANNEL_TOKEN:
        return LINE_LOGIN_CHANNEL_TOKEN, None
    if _liff_token_cache["token"] and time.time() < _liff_token_cache["exp"]:
        return _liff_token_cache["token"], None

    # --- JWT assertion (LINE Login channel) ---
    if LINE_LOGIN_CHANNEL_ID and LINE_LOGIN_ASSERTION_KID and LINE_LOGIN_ASSERTION_PRIVATE_KEY:
        try:
            import jwt as _jwt
            now = int(time.time())
            assertion = _jwt.encode(
                {"iss": LINE_LOGIN_CHANNEL_ID, "sub": LINE_LOGIN_CHANNEL_ID,
                 "aud": "https://api.line.me/", "exp": now + 60 * 25,
                 "token_exp": 60 * 60 * 24 * 30},
                LINE_LOGIN_ASSERTION_PRIVATE_KEY, algorithm="RS256",
                headers={"kid": LINE_LOGIN_ASSERTION_KID})
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post("https://api.line.me/oauth2/v2.1/token", data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                })
            j = r.json()
            if r.status_code == 200 and j.get("access_token"):
                _liff_token_cache["token"] = j["access_token"]
                _liff_token_cache["exp"] = time.time() + max(600, int(j.get("expires_in", 2592000)) - 86400)
                return j["access_token"], None
            return None, f"JWT assertion ไม่ผ่าน ({r.status_code}): {j.get('error_description') or j.get('error') or r.text[:150]}"
        except Exception as e:
            return None, f"JWT assertion error: {e}"

    # --- client_credentials (Messaging API channel เท่านั้น) ---
    if LINE_LOGIN_CHANNEL_ID and LINE_LOGIN_CHANNEL_SECRET:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post("https://api.line.me/v2/oauth/accessToken",
                                 data={"grant_type": "client_credentials",
                                       "client_id": LINE_LOGIN_CHANNEL_ID,
                                       "client_secret": LINE_LOGIN_CHANNEL_SECRET})
            j = r.json()
            if r.status_code == 200 and j.get("access_token"):
                _liff_token_cache["token"] = j["access_token"]
                _liff_token_cache["exp"] = time.time() + max(600, int(j.get("expires_in", 2592000)) - 86400)
                return j["access_token"], None
            err = j.get("error_description") or j.get("error") or r.text[:120]
            if "client_secret" in str(err).lower():
                err += " — channel นี้เป็น LINE Login channel: ต้องตั้ง LINE_LOGIN_ASSERTION_KID + LINE_LOGIN_ASSERTION_PRIVATE_KEY"
            return None, f"ขอ token ไม่สำเร็จ ({r.status_code}): {err}"
        except Exception as e:
            return None, str(e)

    return None, "ยังไม่ได้ตั้งค่าเชื่อม LINE Login channel (LINE_LOGIN_ASSERTION_KID + PRIVATE_KEY)"


@app.get("/api/liff")
async def liff_list(admin=Depends(current_admin)):
    stored = await supa.select("liff_apps", params={"select": "*", "order": "is_primary.desc,updated_at.desc"})
    # sync จาก LINE — ดึงรายการ LIFF ทั้งหมดของ Login channel
    line_apps, sync_err = [], None
    token, terr = await _liff_channel_token()
    if terr:
        sync_err = terr
    elif token:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get("https://api.line.me/liff/v1/apps",
                                headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                line_apps = r.json().get("apps", [])
            elif r.status_code == 401:
                sync_err = "token ไม่ผ่าน — channel secret/id อาจผิด หรือไม่ใช่ Login channel"
            else:
                sync_err = f"{r.status_code}: {r.text[:150]}"
        except Exception as e:
            sync_err = str(e)

    stored_ids = {s["liff_id"] for s in stored}
    merged = []
    for s in stored:
        m = {**_liff_meta(s["liff_id"]), **s, "source": "stored"}
        la = next((a for a in line_apps if a["liffId"] == s["liff_id"]), None)
        if la:
            m["line"] = la
        merged.append(m)
    for a in line_apps:
        if a["liffId"] not in stored_ids:
            merged.append({**_liff_meta(a["liffId"]), "name": a.get("description"),
                           "line": a, "source": "line"})

    return {
        "apps": merged,
        "line_count": len(line_apps),
        "env": {
            "VITE_LIFF_ID": os.environ.get("VITE_LIFF_ID"),
            "LIFF_CHANNEL_ID": LIFF_CHANNEL_ID,
            "LINE_LOGIN_CHANNEL_ID": LINE_LOGIN_CHANNEL_ID,
            "recommendedEndpoint": APP_URL,
        },
        "syncEnabled": bool(token),
        "syncMode": ("token" if LINE_LOGIN_CHANNEL_TOKEN
                     else "jwt" if (LINE_LOGIN_ASSERTION_KID and LINE_LOGIN_ASSERTION_PRIVATE_KEY)
                     else "client_credentials" if LINE_LOGIN_CHANNEL_SECRET else "off"),
        "syncError": sync_err,
    }


@app.post("/api/liff")
async def liff_save(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    lid = (b.get("liffId") or "").strip()
    if not re.fullmatch(r"\d{9,12}-[0-9a-zA-Z]{6,12}", lid):
        raise HTTPException(400, "LIFF ID ผิดรูปแบบ (เช่น 2009830584-koG1QjOD)")
    row = {
        "liff_id": lid,
        "name": b.get("name"),
        "size": b.get("size"),
        "endpoint_url": b.get("endpointUrl"),
        "description": b.get("description"),
        "scopes": b.get("scopes", []),
        "bot_prompt": b.get("botPrompt"),
        "features": b.get("features", {}),
        "module_mode": bool(b.get("moduleMode")),
        "is_primary": bool(b.get("isPrimary")),
        "channel_id": lid.split("-")[0],
        "note": b.get("note"),
        "created_by": admin["userId"],
        "updated_at": NOW(),
    }
    if b.get("isPrimary"):
        await supa.update("liff_apps", {"is_primary": False}, {"is_primary": "eq.true"})
    await supa.upsert("liff_apps", row, on_conflict="liff_id")
    return {"ok": True, **_liff_meta(lid)}


@app.delete("/api/liff/{liff_id}")
async def liff_delete(liff_id: str, admin=Depends(current_admin)):
    await supa.delete("liff_apps", {"liff_id": f"eq.{liff_id}"})
    return {"ok": True}


@app.post("/api/target/resolve")
async def resolve_target(req: Request, admin=Depends(current_admin)):
    """คืน userId list ตาม target (all/none/tag/segment/filter) — ให้ frontend เอาไป chunk + แสดง %"""
    b = await req.json()
    tg = b.get("target", "all")
    if tg == "segment":
        seg = await supa.select("segments", params={"id": f"eq.{b.get('segmentId')}", "select": "filter", "limit": "1"})
        uids = await _uids_by_filter(seg[0]["filter"] if seg else {})
    elif tg == "filter":
        uids = await _uids_by_filter(b.get("filter", {}))
    elif tg == "none":
        uids = await _uids_by_filter({"noMenu": True})
    elif tg == "tag":
        uids = await _uids_by_filter({"tag": b.get("tag", "")})
    elif tg == "list":
        uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
    else:
        uids = await _uids_by_filter({})
    return {"count": len(uids), "userIds": uids}


@app.get("/api/segments")
async def list_segments(admin=Depends(current_admin)):
    return {"segments": await supa.select("segments", params={"select": "*", "order": "updated_at.desc"})}


@app.post("/api/segments")
async def save_segment(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    cnt = len(await _uids_by_filter(b.get("filter", {})))
    row = {"name": b["name"], "description": b.get("description"),
           "filter": b.get("filter", {}), "last_count": cnt,
           "created_by": admin["userId"], "updated_at": NOW()}
    if b.get("id"):
        await supa.update("segments", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"], "count": cnt}
    r = await supa.insert("segments", row)
    return {"ok": True, "segment": r[0] if r else None, "count": cnt}


@app.get("/api/segments/{sid}/count")
async def segment_count(sid: int, admin=Depends(current_admin)):
    rows = await supa.select("segments", params={"id": f"eq.{sid}", "select": "filter", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบ segment")
    uids = await _uids_by_filter(rows[0]["filter"])
    await supa.update("segments", {"last_count": len(uids)}, {"id": f"eq.{sid}"})
    return {"count": len(uids)}


@app.delete("/api/segments/{sid}")
async def delete_segment(sid: int, admin=Depends(current_admin)):
    await supa.delete("segments", {"id": f"eq.{sid}"})
    return {"ok": True}


# ============================================================
# NARROWCAST (ส่งตาม demographic ของ LINE)
# ============================================================
@app.post("/api/message/narrowcast")
async def msg_narrowcast(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    msgs = _normalize_messages(b.get("messages", []))
    demo = b.get("demographic")   # LINE filter object (age/gender/area/...)
    recipient = b.get("recipient")  # audience / redelivery object
    limit = b.get("limit")
    filter_ = {"demographic": demo} if demo else None
    bid, msgs = await _broadcast_begin(admin["userId"], "narrowcast", msgs, None)
    code, txt, rid = await line.narrowcast(msgs, recipient=recipient, filter_=filter_, limit=limit)
    status = "sent" if code in (200, 202) else "failed"
    await _broadcast_finish(bid, status=status, line_request_id=rid,
                            error=None if status == "sent" else txt[:300])
    if code not in (200, 202):
        raise HTTPException(400, txt)
    return {"ok": True, "requestId": rid}


@app.get("/api/message/narrowcast/progress")
async def narrowcast_progress(admin=Depends(current_admin), requestId: str = ""):
    r = await line._req("GET", "/v2/bot/message/progress/narrowcast", params={"requestId": requestId})
    return r.json()


# ============================================================
# SLIPS (สลิปโอนเงิน) + SETTINGS
# ============================================================
@app.get("/api/slips")
async def list_slips(admin=Depends(current_admin), status: str = "", limit: int = 100):
    params = {"select": "*", "order": "created_at.desc", "limit": str(min(limit, 300))}
    if status:
        params["status"] = f"eq.{status}"
    slips = await supa.select("slips", params=params)
    # แนบชื่อ user
    uids = list({s["line_user_id"] for s in slips})
    names = {}
    if uids:
        for r in await supa.select("line_users", params={
            "select": "line_user_id,display_name,picture_url",
            "line_user_id": f"in.({','.join(uids)})", "limit": "500"}):
            names[r["line_user_id"]] = r
    for s in slips:
        s["user"] = names.get(s["line_user_id"], {})
    new_count = await supa.count("slips", {"status": "eq.new"})
    return {"slips": slips, "new_count": new_count}


@app.post("/api/slips/{sid}")
async def update_slip(sid: int, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    patch = {k: b[k] for k in ("status", "note", "amount") if k in b}
    if b.get("status") in ("verified", "rejected"):
        patch["reviewed_by"] = admin["userId"]
        patch["reviewed_at"] = NOW()
    await supa.update("slips", patch, {"id": f"eq.{sid}"})
    row = None
    if b.get("replyUser") or b.get("status") == "verified":
        r = await supa.select("slips", params={
            "id": f"eq.{sid}", "select": "line_user_id,expected_course,amount,bank,ref,slip_date", "limit": "1"})
        row = r[0] if r else None
    # ตอบ user ถ้าขอ
    if b.get("replyUser") and row:
        msg = ("✅ ตรวจสอบสลิปเรียบร้อยแล้ว ขอบคุณครับ 🙏"
               if b["status"] == "verified" else
               "สลิปที่ส่งมายังตรวจสอบไม่ผ่าน รบกวนส่งใหม่หรือติดต่อแอดมินครับ 🙏")
        await line.push(row["line_user_id"], [{"type": "text", "text": b.get("replyText") or msg}])
    # เข้า automation trigger=slip_verified (แอดมินกดผ่านเอง)
    if b.get("status") == "verified" and row:
        try:
            await _enroll_automations("slip_verified", [row["line_user_id"]],
                                      {"course": row.get("expected_course")})
            await _mark_registration_paid(row["line_user_id"], row.get("expected_course"))
            await _send_receipt(row["line_user_id"], {
                "id": sid, "amount": b.get("amount") or row.get("amount"),
                "course": row.get("expected_course"), "bank": row.get("bank"),
                "ref": row.get("ref"), "date": row.get("slip_date")})
        except Exception as e:
            print("slip_verified enroll error:", e)
    return {"ok": True}


@app.get("/api/payment-accounts")
async def list_pay_accounts(admin=Depends(current_admin)):
    return {"accounts": await supa.select("payment_accounts", params={"select": "*", "order": "course"})}


@app.post("/api/payment-accounts")
async def save_pay_account(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    row = {k: b[k] for k in ("course", "bank", "account_no", "account_name", "price", "full_price", "active") if k in b}
    if b.get("id"):
        await supa.update("payment_accounts", row, {"id": f"eq.{b['id']}"})
    else:
        await supa.insert("payment_accounts", row)
    return {"ok": True}


@app.delete("/api/payment-accounts/{aid}")
async def del_pay_account(aid: int, admin=Depends(current_admin)):
    await supa.delete("payment_accounts", {"id": f"eq.{aid}"})
    return {"ok": True}


# ============================================================
# REGISTRATIONS (รายชื่อผู้ลงทะเบียนเรียน)
# ============================================================
def _norm_course(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    su = s.upper().replace(" ", "")
    base = next((c for c in ("FC", "IC", "PC", "AC") if su.startswith(c)), None)
    if not base:
        return s[:24]
    code = base + "70"
    if any(x in s for x in ("รุ่น 2", "รุ่น2", "รุ่น๒", "รุ่นที่2", "รุ่นที่ 2")):
        return code + "-2"
    if any(x in s for x in ("รุ่น 1", "รุ่น1", "รุ่น๑", "รุ่นที่1", "รุ่นที่ 1")):
        return code + "-1"
    return code


_REG_COLS = {
    "timestamp": "form_ts", "regid": "reg_id", "uid": "line_user_id", "name": "name",
    "tel": "tel", "สังกัด": "org", "หลักสูตร": "course_raw", "email": "email", "job": "job",
    "file": "slip_url", "ผ่าน": "passed",
}

_REG_COURSES_DEFAULT = ["FC70", "IC70", "PC70-1", "PC70-2"]
_member_tok_cache: dict = {}


async def _reg_courses() -> list:
    v = await _get_setting("reg_courses", None)
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    return _REG_COURSES_DEFAULT


async def _verify_member_token(id_token: str) -> dict | None:
    """ตรวจ LIFF id_token ของหน้าลงทะเบียนสมาชิก (channel REG_LIFF_CHANNEL_ID) — cache 5 นาที"""
    if not id_token:
        return None
    hit = _member_tok_cache.get(id_token)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post("https://api.line.me/oauth2/v2.1/verify",
                             data={"id_token": id_token, "client_id": REG_LIFF_CHANNEL_ID})
        if r.status_code != 200:
            return None
        p = r.json()
        if str(p.get("aud")) != str(REG_LIFF_CHANNEL_ID):
            return None
        if p.get("exp") and time.time() > p["exp"]:
            return None
        u = {"userId": p["sub"], "name": p.get("name", ""), "picture": p.get("picture", "")}
        _member_tok_cache[id_token] = (time.time() + 300, u)
        return u
    except Exception as e:
        print("member token verify error:", e)
        return None


@app.post("/api/public/reg-check")
async def public_reg_check(req: Request):
    """หน้าลงทะเบียนสมาชิกเรียก: ตรวจว่า userId นี้มีในทะเบียนแล้วหรือยัง"""
    b = await req.json()
    u = await _verify_member_token(b.get("idToken"))
    if not u:
        raise HTTPException(401, "ยืนยันตัวตน LINE ไม่สำเร็จ")
    regs = await supa.select("registrations", params={
        "line_user_id": f"eq.{u['userId']}",
        "select": "id,course,course_raw,paid,name,approved,created_at",
        "order": "created_at.desc"})
    return {
        "registered": bool(regs),
        "userId": u["userId"], "displayName": u.get("name"), "picture": u.get("picture"),
        "registrations": regs,
        "courses": await _reg_courses(),
        "nextUrl": await _get_setting("reg_next_url", ""),
    }


@app.post("/api/public/reg-submit")
async def public_reg_submit(req: Request):
    """หน้าลงทะเบียนสมาชิกส่งฟอร์ม -> upsert registration + tag user + แจ้งแอดมิน"""
    b = await req.json()
    u = await _verify_member_token(b.get("idToken"))
    if not u:
        raise HTTPException(401, "ยืนยันตัวตน LINE ไม่สำเร็จ")
    name = (b.get("name") or "").strip()
    tel = (b.get("tel") or "").strip()
    course_raw = (b.get("course") or "").strip()
    course = _norm_course(course_raw)
    if not name or not tel or not course:
        raise HTTPException(400, "กรอก ชื่อ-นามสกุล / เบอร์โทร / หลักสูตร ให้ครบ")
    _rate_limit(f"reg:{u['userId']}", limit=6, window=300)
    row = {"line_user_id": u["userId"], "course": course, "course_raw": course_raw,
           "name": name, "tel": tel, "org": (b.get("org") or "").strip() or None,
           "email": (b.get("email") or "").strip() or None, "source": "liff", "updated_at": NOW()}
    await supa.upsert("registrations", row, on_conflict="line_user_id,course")
    try:
        ex = await supa.select("line_users", params={
            "select": "tags", "line_user_id": f"eq.{u['userId']}", "limit": "1"})
        cur = set((ex[0].get("tags") if ex else []) or [])
        new = cur | {"ลงทะเบียน", course}
        patch = {"line_user_id": u["userId"], "tags": sorted(new), "updated_at": NOW()}
        if not ex:
            patch.update({"source": "registration", "is_following": True,
                          "display_name": _clean_str(u.get("name"))})
        if new != cur or not ex:
            await supa.upsert("line_users", [patch], on_conflict="line_user_id")
    except Exception as e:
        print("reg-submit tag error:", e)
    await alert_admin("📝 ลงทะเบียนใหม่ (LIFF)",
                      f"{name}\nหลักสูตร {course} · {b.get('org') or '-'}\nโทร {tel}",
                      throttle_key=f"reg_{u['userId']}_{course}")
    return {"ok": True, "course": course, "nextUrl": await _get_setting("reg_next_url", "")}


@app.post("/api/registrations/import")
async def registrations_import(req: Request, admin=Depends(current_admin)):
    """วางข้อความ TSV จากฟอร์มลงทะเบียน -> upsert registrations + tag line_users ตามคอร์ส"""
    b = await req.json()
    text = b.get("text") or ""
    tag_users = b.get("tagUsers", True)
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        raise HTTPException(400, "ไม่พบข้อมูล (วางทั้งหัวตารางและแถวข้อมูล)")

    header = [h.strip().lower() for h in lines[0].split("\t")]
    idx = {}
    for i, h in enumerate(header):
        for key, col in _REG_COLS.items():
            if h == key or h == key.lower():
                idx[col] = i
    if "line_user_id" not in idx:
        raise HTTPException(400, "ไม่พบคอลัมน์ UID ในหัวตาราง")
    # หา col "จ่าย" แยก (ชื่อซ้ำกับ substring)
    paid_i = next((i for i, h in enumerate(header) if h in ("จ่าย", "จ่ายแล้ว")), None)
    status_i = next((i for i, h in enumerate(header) if h == "status"), None)

    regs, seen = [], set()
    for ln in lines[1:]:
        f = ln.split("\t")
        uid = (f[idx["line_user_id"]].strip() if idx["line_user_id"] < len(f) else "")
        if not re.fullmatch(r"U[0-9a-f]{32}", uid):
            continue
        course_raw = (f[idx["course_raw"]].strip() if "course_raw" in idx and idx["course_raw"] < len(f) else "")
        course = _norm_course(course_raw)
        k = (uid, course)
        if k in seen:
            continue
        seen.add(k)
        row = {"line_user_id": uid, "course": course, "course_raw": course_raw, "source": "import",
               "updated_at": NOW()}
        for col, i in idx.items():
            if col in ("line_user_id", "course_raw"):
                continue
            row[col] = (f[i].strip() if i < len(f) else "") or None
        if paid_i is not None and paid_i < len(f):
            row["paid"] = "จ่าย" in (f[paid_i] or "")
        if status_i is not None and status_i < len(f):
            st = (f[status_i] or "").strip()
            row["approved"] = True if st in ("อนุมัติ", "ผ่าน") else (False if st == "ไม่อนุมัติ" else None)
        regs.append(row)

    if not regs:
        raise HTTPException(400, "ไม่พบแถวที่มี UID ถูกต้อง (U + 32 hex)")
    if len(regs) > 2000:
        raise HTTPException(400, "เกิน 2000 แถว/ครั้ง — แบ่งไฟล์เป็นหลายส่วน")

    # 1) upsert registrations (สำคัญสุด)
    for i in range(0, len(regs), 200):
        await supa.upsert("registrations", regs[i:i + 200], on_conflict="line_user_id,course")

    # 2) tag line_users — best effort, chunk เล็ก (URL in.() ยาวไป PostgREST ปฏิเสธ)
    users_touched, tag_err = 0, None
    if tag_users:
        by_uid: dict[str, dict] = {}
        for rr in regs:
            u = by_uid.setdefault(rr["line_user_id"], {"courses": set(), "paid": False})
            if rr.get("course"):
                u["courses"].add(rr["course"])
            if rr.get("paid"):
                u["paid"] = True
        uids = list(by_uid.keys())
        for i in range(0, len(uids), 80):
            chunk = uids[i:i + 80]
            try:
                existing = {x["line_user_id"]: x for x in await supa.select("line_users", params={
                    "select": "line_user_id,tags", "line_user_id": f"in.({','.join(chunk)})", "limit": "200"})}
                patch = []
                for uid in chunk:
                    info = by_uid[uid]
                    cur = set((existing.get(uid) or {}).get("tags") or [])
                    new = set(cur) | {"ลงทะเบียน"} | info["courses"]
                    if info["paid"]:
                        new.add("จ่ายแล้ว")
                    row = {"line_user_id": uid, "tags": sorted(new), "updated_at": NOW()}
                    if uid not in existing:
                        row["source"] = "registration"
                        row["is_following"] = True
                    if new != cur or uid not in existing:
                        patch.append(row)
                if patch:
                    await supa.upsert("line_users", patch, on_conflict="line_user_id")
                    users_touched += len(patch)
            except Exception as e:
                tag_err = str(e)[:120]

    from collections import Counter
    by_course = dict(Counter(r.get("course") or "?" for r in regs))
    try:
        await supa.log_operation(admin["userId"], "registrations.import",
                                 {"parsed": len(lines) - 1}, {"valid": len(regs), "users": users_touched})
    except Exception:
        pass
    return {"parsed": len(lines) - 1, "valid": len(regs), "by_course": by_course,
            "paid": sum(1 for r in regs if r.get("paid")), "users_tagged": users_touched,
            "tag_error": tag_err}


@app.get("/api/registrations")
async def registrations_list(admin=Depends(current_admin), course: str = "", paid: str = "",
                             q: str = "", limit: int = 100, offset: int = 0):
    params = {"select": "*", "order": "created_at.desc",
              "limit": str(min(limit, 500)), "offset": str(max(offset, 0))}
    if course:
        params["course"] = f"eq.{course}"
    if paid in ("true", "false"):
        params["paid"] = f"eq.{paid}"
    if q:
        params["or"] = f"(name.ilike.*{q}*,tel.ilike.*{q}*,email.ilike.*{q}*,org.ilike.*{q}*,line_user_id.ilike.*{q}*)"
    rows = await supa.select("registrations", params=params)
    cnt_params = {k: v for k, v in params.items() if k in ("course", "paid", "or")}
    total = await supa.count("registrations", cnt_params)
    return {"registrations": rows, "total": total, "limit": limit, "offset": offset}


# ============================================================
# CLASSES (คลาส / รุ่น) + ATTENDANCE (เช็คชื่อ)
# ============================================================
@app.get("/api/classes")
async def classes_list(admin=Depends(current_admin), course: str = "", status: str = ""):
    params = {"select": "*", "order": "start_date.desc.nullslast,id.desc", "limit": "200"}
    if course:
        params["course"] = f"eq.{course}"
    if status:
        params["status"] = f"eq.{status}"
    try:
        classes = await supa.select("classes", params=params)
    except Exception:
        return {"classes": [], "schema_missing": True,
                "hint": "ยังไม่ได้สร้างตาราง classes — รัน migration 0009 ใน Supabase SQL Editor"}
    if classes:
        ids = [c["id"] for c in classes]
        counts: dict = {}
        for r in await supa.select("registrations", params={
                "select": "class_id", "class_id": f"in.({','.join(map(str, ids))})", "limit": "5000"}):
            counts[r["class_id"]] = counts.get(r["class_id"], 0) + 1
        for c in classes:
            c["enrolled"] = counts.get(c["id"], 0)
    return {"classes": classes}


@app.post("/api/classes")
async def class_save(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    row = {k: b[k] for k in ("course", "name", "cohort", "start_date", "end_date", "schedule",
                             "zoom_url", "materials_url", "capacity", "status", "note") if k in b}
    if not row.get("course"):
        raise HTTPException(400, "ต้องระบุหลักสูตร")
    row["updated_at"] = NOW()
    if b.get("id"):
        await supa.update("classes", row, {"id": f"eq.{b['id']}"})
        return {"ok": True, "id": b["id"]}
    row["created_by"] = admin["userId"]
    r = await supa.insert("classes", row)
    return {"ok": True, "class": r[0] if r else None}


@app.delete("/api/classes/{cid}")
async def class_delete(cid: int, admin=Depends(current_admin)):
    await supa.update("registrations", {"class_id": None}, {"class_id": f"eq.{cid}"})
    await supa.delete("classes", {"id": f"eq.{cid}"})
    return {"ok": True}


@app.get("/api/classes/{cid}")
async def class_detail(cid: int, admin=Depends(current_admin)):
    rows = await supa.select("classes", params={"id": f"eq.{cid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบ")
    cls = rows[0]
    base = (cls.get("course") or "")[:2].upper()
    roster = await supa.select("registrations", params={
        "select": "id,line_user_id,name,tel,course,course_raw,paid,class_id,exam_passed",
        "class_id": f"eq.{cid}", "order": "name.asc", "limit": "1000"})
    # ผู้ที่ยังไม่ถูก assign เข้าคลาสไหน แต่คอร์สตรง
    avail = await supa.select("registrations", params={
        "select": "id,line_user_id,name,tel,course_raw,paid", "class_id": "is.null",
        "course": f"like.{base}*", "limit": "500"})
    att = await supa.select("attendance", params={
        "select": "line_user_id,session,present", "class_id": f"eq.{cid}", "limit": "5000"})
    sessions = sorted({a["session"] for a in att})
    att_map: dict = {}
    for a in att:
        att_map.setdefault(a["line_user_id"], {})[a["session"]] = a["present"]
    return {"class": cls, "roster": roster, "available": avail,
            "sessions": sessions, "attendance": att_map}


@app.post("/api/classes/{cid}/assign")
async def class_assign(cid: int, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    ids = [int(x) for x in b.get("registrationIds", []) if str(x).isdigit()]
    if not ids:
        raise HTTPException(400, "ไม่มีรายการ")
    val = cid if b.get("mode") != "remove" else None
    await supa.update("registrations", {"class_id": val, "updated_at": NOW()},
                      {"id": f"in.({','.join(map(str, ids))})"})
    return {"ok": True, "changed": len(ids)}


@app.post("/api/classes/{cid}/attendance")
async def class_attendance(cid: int, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    session = int(b.get("session", 1))
    recs = b.get("records", [])
    rows = [{"class_id": cid, "line_user_id": r["line_user_id"], "session": session,
             "present": bool(r.get("present", True)), "marked_by": admin["userId"]}
            for r in recs if r.get("line_user_id")]
    if rows:
        await supa.upsert("attendance", rows, on_conflict="class_id,line_user_id,session")
    return {"ok": True, "marked": len(rows)}


@app.post("/api/classes/{cid}/message")
async def class_message(cid: int, req: Request, admin=Depends(current_admin)):
    rows = await supa.select("classes", params={"id": f"eq.{cid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบคลาส")
    cls = rows[0]
    b = await req.json()
    roster = await supa.select("registrations", params={
        "select": "line_user_id,name,course_raw", "class_id": f"eq.{cid}", "limit": "1000"})
    uids = list({r["line_user_id"] for r in roster if r.get("line_user_id")})
    if not uids:
        raise HTTPException(400, "คลาสนี้ยังไม่มีนักเรียน")
    text = (b.get("text") or "")
    text = (text.replace("{zoom}", cls.get("zoom_url") or "")
                .replace("{materials}", cls.get("materials_url") or "")
                .replace("{class}", cls.get("name") or cls.get("course") or "")
                .replace("{schedule}", cls.get("schedule") or "")
                .replace("{start}", str(cls.get("start_date") or "")))
    msgs = [{"type": "text", "text": text}]
    sent = failed = 0
    for i in range(0, len(uids), 500):
        code, *_ = await line.multicast(uids[i:i + 500], msgs)
        if code == 200:
            sent += len(uids[i:i + 500])
        else:
            failed += len(uids[i:i + 500])
    await supa.insert("broadcasts", {"actor": admin["userId"], "kind": "multicast",
                                     "target_count": len(uids), "messages": msgs,
                                     "status": "sent" if not failed else "failed"})
    return {"ok": not failed, "sent": sent, "failed": failed}


# ============================================================
# TASKS (งาน / ติดตาม)
# ============================================================
@app.get("/api/tasks")
async def tasks_list(admin=Depends(current_admin), status: str = "open", assignee: str = "",
                     uid: str = "", overdue: int = 0, limit: int = 200):
    params = {"select": "*", "order": "due_at.asc.nullslast,created_at.desc",
              "limit": str(min(limit, 500))}
    if status and status != "all":
        params["status"] = f"eq.{status}"
    if assignee == "me":
        params["assigned_to"] = f"eq.{admin['userId']}"
    elif assignee:
        params["assigned_to"] = f"eq.{assignee}"
    if uid:
        params["line_user_id"] = f"eq.{uid}"
    if overdue:
        params["due_at"] = f"lt.{NOW()}"
        params["status"] = "eq.open"
    try:
        rows = await supa.select("tasks", params=params)
    except Exception:
        return {"tasks": [], "schema_missing": True,
                "hint": "ยังไม่ได้สร้างตาราง tasks — รัน migration 0008 ใน Supabase SQL Editor"}
    uids = list({r["line_user_id"] for r in rows if r.get("line_user_id")})
    names = {}
    if uids:
        for i in range(0, len(uids), 80):
            for u in await supa.select("line_users", params={
                    "select": "line_user_id,display_name,picture_url",
                    "line_user_id": f"in.({','.join(uids[i:i+80])})", "limit": "200"}):
                names[u["line_user_id"]] = u
    for r in rows:
        r["user"] = names.get(r.get("line_user_id"))
    return {"tasks": rows}


@app.get("/api/tasks/summary")
async def tasks_summary(admin=Depends(current_admin)):
    now = NOW()
    today0 = dt.date.today().isoformat()
    c = await _gather_dict(
        open=supa.count("tasks", {"status": "eq.open"}),
        overdue=supa.count("tasks", {"status": "eq.open", "due_at": f"lt.{now}"}),
        mine=supa.count("tasks", {"status": "eq.open", "assigned_to": f"eq.{admin['userId']}"}),
        done_today=supa.count("tasks", {"status": "eq.done", "done_at": f"gte.{today0}"}),
    )
    return c


@app.post("/api/tasks")
async def task_create(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    title = (b.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "ต้องมีชื่องาน")
    row = {"title": title, "detail": b.get("detail"), "line_user_id": b.get("lineUserId") or b.get("line_user_id"),
           "due_at": b.get("dueAt") or b.get("due_at"), "priority": b.get("priority", "normal"),
           "assigned_to": b.get("assignedTo") or b.get("assigned_to") or admin["userId"],
           "tag": b.get("tag"), "created_by": admin["userId"]}
    r = await supa.insert("tasks", row)
    return {"ok": True, "task": r[0] if r else None}


@app.post("/api/tasks/bulk")
async def tasks_bulk(req: Request, admin=Depends(current_admin)):
    """สร้างงานหลายชิ้นจากรายชื่อ userId (เช่น 'ตามจ่าย' จากหน้ากระทบยอด)"""
    b = await req.json()
    title = (b.get("title") or "").strip()
    uids = [u.strip() for u in b.get("userIds", []) if u.strip()]
    if not title or not uids:
        raise HTTPException(400, "ต้องมีชื่องาน + รายชื่อ")
    rows = [{"title": title, "line_user_id": u, "detail": b.get("detail"),
             "due_at": b.get("dueAt"), "tag": b.get("tag"), "priority": b.get("priority", "normal"),
             "assigned_to": b.get("assignedTo") or admin["userId"], "created_by": admin["userId"]}
            for u in uids]
    for i in range(0, len(rows), 200):
        await supa.insert("tasks", rows[i:i + 200])
    await supa.log_operation(admin["userId"], "tasks.bulk", {"count": len(rows), "title": title}, None)
    return {"ok": True, "created": len(rows)}


@app.patch("/api/tasks/{tid}")
async def task_update(tid: int, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    patch = {k: b[k] for k in ("title", "detail", "due_at", "priority", "assigned_to", "tag", "status") if k in b}
    for camel, snake in (("dueAt", "due_at"), ("assignedTo", "assigned_to")):
        if camel in b:
            patch[snake] = b[camel]
    if b.get("status") == "done":
        patch["done_at"] = NOW()
    patch["updated_at"] = NOW()
    await supa.update("tasks", patch, {"id": f"eq.{tid}"})
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
async def task_delete(tid: int, admin=Depends(current_admin)):
    await supa.delete("tasks", {"id": f"eq.{tid}"})
    return {"ok": True}


@app.api_route("/api/cron/task-reminders", methods=["GET", "POST"])
async def cron_task_reminders(request: Request):
    """แจ้งเตือนแอดมินแต่ละคนถ้ามีงานเกินกำหนด (push เข้า LINE)"""
    _check_cron_key(request)
    overdue = await supa.select("tasks", params={
        "select": "assigned_to,title,due_at", "status": "eq.open", "due_at": f"lt.{NOW()}", "limit": "500"})
    by_admin: dict[str, list] = {}
    for t in overdue:
        if t.get("assigned_to"):
            by_admin.setdefault(t["assigned_to"], []).append(t)
    sent = 0
    for uid, items in by_admin.items():
        txt = f"⏰ งานเกินกำหนด {len(items)} รายการ\n" + "\n".join(
            f"• {i['title']}" for i in items[:8]) + (f"\n…และอีก {len(items)-8}" if len(items) > 8 else "")
        txt += f"\n\nดูทั้งหมด: {APP_URL}/tasks"
        try:
            code, *_ = await line.push(uid, [{"type": "text", "text": txt}])
            sent += (code == 200)
        except Exception:
            pass
    return {"ok": True, "admins_notified": sent, "overdue": len(overdue)}


@app.get("/api/reconcile")
async def reconcile(admin=Depends(current_admin)):
    """กระทบยอด: ทะเบียน vs สลิปที่ยืนยัน vs สถานะจ่าย"""
    regs = await supa.select_all("registrations", params={
        "select": "id,line_user_id,course,course_raw,name,tel,org,paid,created_at"})
    slips = await supa.select_all("slips", params={
        "select": "id,line_user_id,expected_course,amount,created_at", "status": "eq.verified"})
    accts = await supa.select("payment_accounts", params={"select": "course,price,full_price"})

    def _price(course):
        base = (course or "")[:2].upper()
        for a in accts:
            if str(a.get("course") or "").upper().startswith(base):
                return float(a.get("price") or 0)
        return 0.0

    slips_by_uid: dict[str, list] = {}
    for s in slips:
        slips_by_uid.setdefault(s["line_user_id"], []).append(s)

    def _slip_for(uid, course):
        base = (course or "")[:2].upper()
        for s in slips_by_uid.get(uid, []):
            if base and str(s.get("expected_course") or "").upper().startswith(base) and not s.get("_used"):
                return s
        return None

    course_map: dict[str, dict] = {}
    unpaid, paid_not_marked = [], []
    for r in regs:
        c = r.get("course") or "?"
        cm = course_map.setdefault(c, {"course": c, "registered": 0, "paid": 0,
                                       "slip_verified": 0, "revenue": 0.0, "unpaid": 0})
        cm["registered"] += 1
        s = _slip_for(r["line_user_id"], c)
        if s:
            s["_used"] = True
            cm["slip_verified"] += 1
            cm["revenue"] += float(s.get("amount") or 0)
        if r.get("paid"):
            cm["paid"] += 1
        else:
            cm["unpaid"] += 1
            item = {"id": r["id"], "line_user_id": r["line_user_id"], "name": r.get("name"),
                    "tel": r.get("tel"), "org": r.get("org"), "course": c}
            if s:
                item["slip_amount"] = s.get("amount")
                item["slip_date"] = s.get("created_at")
                paid_not_marked.append(item)
            else:
                unpaid.append(item)

    reg_keys = {(r["line_user_id"], (r.get("course") or "")[:2].upper()) for r in regs}
    slip_no_reg = []
    for s in slips:
        base = str(s.get("expected_course") or "").upper()[:2]
        if not base:
            slip_no_reg.append({"id": s["id"], "line_user_id": s["line_user_id"],
                                "amount": s.get("amount"), "created_at": s.get("created_at"),
                                "reason": "สลิปไม่ระบุคอร์ส"})
        elif (s["line_user_id"], base) not in reg_keys:
            slip_no_reg.append({"id": s["id"], "line_user_id": s["line_user_id"],
                                "amount": s.get("amount"), "created_at": s.get("created_at"),
                                "course": s.get("expected_course"), "reason": "ไม่มีทะเบียนคอร์สนี้"})

    # แนบชื่อ user ให้ slip_no_reg
    uids = list({x["line_user_id"] for x in slip_no_reg})
    if uids:
        for i in range(0, len(uids), 60):
            names = {u["line_user_id"]: u.get("display_name") for u in await supa.select("line_users", params={
                "select": "line_user_id,display_name", "line_user_id": f"in.({','.join(uids[i:i+60])})", "limit": "200"})}
            for x in slip_no_reg:
                if x["line_user_id"] in names:
                    x["name"] = names[x["line_user_id"]]

    for cm in course_map.values():
        cm["expected_revenue"] = round(_price(cm["course"]) * cm["registered"])
        cm["revenue"] = round(cm["revenue"])

    return {
        "by_course": sorted(course_map.values(), key=lambda x: -x["registered"]),
        "totals": {
            "registered": len(regs),
            "paid_marked": sum(1 for r in regs if r.get("paid")),
            "slip_verified": len(slips),
            "revenue": round(sum(float(s.get("amount") or 0) for s in slips)),
            "expected_revenue": round(sum(_price(r.get("course")) for r in regs)),
        },
        "issues": {
            "unpaid": unpaid[:300], "unpaid_count": len(unpaid),
            "paid_not_marked": paid_not_marked[:300], "paid_not_marked_count": len(paid_not_marked),
            "slip_no_reg": slip_no_reg[:300], "slip_no_reg_count": len(slip_no_reg),
        },
    }


@app.get("/api/reports/revenue")
async def report_revenue(admin=Depends(current_admin), frm: str = "", to: str = "",
                         group: str = "course", format: str = ""):
    """รายงานรายได้จากสลิปที่ verified — group: course | bank | day | month"""
    params = {"select": "amount,bank,expected_course,status,reviewed_at,created_at,slip_date",
              "status": "eq.verified"}
    slips = await supa.select_all("slips", params=params)
    fromd = frm or "0000"
    tod = to or "9999"

    def _d(s):
        return (s.get("reviewed_at") or s.get("slip_date") or s.get("created_at") or "")[:10]

    rows: dict[str, dict] = {}
    total = 0.0
    for s in slips:
        d = _d(s)
        if not (fromd <= d <= tod):
            continue
        amt = float(s.get("amount") or 0)
        total += amt
        if group == "bank":
            k = s.get("bank") or "ไม่ระบุ"
        elif group == "day":
            k = d
        elif group == "month":
            k = d[:7]
        else:
            k = s.get("expected_course") or "ไม่ระบุ"
        g = rows.setdefault(k, {"key": k, "count": 0, "amount": 0.0})
        g["count"] += 1
        g["amount"] += amt
    out = sorted(rows.values(), key=lambda x: (x["key"] if group in ("day", "month") else -x["amount"]))
    for r in out:
        r["amount"] = round(r["amount"])

    if format == "csv":
        lines = ["key,count,amount"] + [f'"{r["key"]}",{r["count"]},{r["amount"]}' for r in out]
        lines.append(f'"รวม",{sum(r["count"] for r in out)},{round(total)}')
        from fastapi.responses import Response
        return Response("﻿" + "\r\n".join(lines), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="revenue-{group}-{dt.date.today()}.csv"'})
    return {"group": group, "from": frm, "to": to, "rows": out,
            "total": round(total), "count": sum(r["count"] for r in out)}


@app.post("/api/reconcile/mark-paid")
async def reconcile_mark_paid(req: Request, admin=Depends(current_admin)):
    """mark registrations เป็นจ่ายแล้วเป็นชุด (จาก paid_not_marked)"""
    b = await req.json()
    ids = [int(x) for x in b.get("ids", []) if str(x).isdigit()]
    if not ids:
        raise HTTPException(400, "ไม่มี id")
    await supa.update("registrations", {"paid": True, "updated_at": NOW()},
                      {"id": f"in.({','.join(map(str, ids))})"})
    # tag จ่ายแล้ว
    regs = await supa.select("registrations", params={
        "select": "line_user_id", "id": f"in.({','.join(map(str, ids))})", "limit": "500"})
    uids = list({r["line_user_id"] for r in regs if r.get("line_user_id")})
    for i in range(0, len(uids), 60):
        chunk = uids[i:i + 60]
        try:
            ex = {u["line_user_id"]: set(u.get("tags") or []) for u in await supa.select("line_users", params={
                "select": "line_user_id,tags", "line_user_id": f"in.({','.join(chunk)})", "limit": "200"})}
            patch = [{"line_user_id": u, "tags": sorted(ex.get(u, set()) | {"จ่ายแล้ว"}), "updated_at": NOW()}
                     for u in chunk if "จ่ายแล้ว" not in ex.get(u, set())]
            if patch:
                await supa.upsert("line_users", patch, on_conflict="line_user_id")
        except Exception as e:
            print("mark-paid tag error:", e)
    await supa.log_operation(admin["userId"], "reconcile.mark_paid", {"count": len(ids)}, None)
    return {"ok": True, "marked": len(ids)}


@app.get("/api/registrations/summary")
async def registrations_summary(admin=Depends(current_admin)):
    rows = await supa.select_all("registrations", params={"select": "course,paid,approved,line_user_id"})
    from collections import Counter
    courses: dict[str, dict] = {}
    for r in rows:
        c = r.get("course") or "?"
        d = courses.setdefault(c, {"course": c, "count": 0, "paid": 0, "unpaid": 0})
        d["count"] += 1
        if r.get("paid"):
            d["paid"] += 1
        else:
            d["unpaid"] += 1
    return {
        "total": len(rows),
        "paid": sum(1 for r in rows if r.get("paid")),
        "people": len({r["line_user_id"] for r in rows if r.get("line_user_id")}),
        "by_course": sorted(courses.values(), key=lambda x: -x["count"]),
    }


@app.get("/api/registrations/{rid}")
async def registration_detail(rid: int, admin=Depends(current_admin)):
    rows = await supa.select("registrations", params={"id": f"eq.{rid}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "ไม่พบ")
    reg = rows[0]
    uid = reg.get("line_user_id")
    user, slips_rows, other_regs = None, [], []
    if uid:
        u = await supa.select("line_users", params={
            "select": "line_user_id,display_name,picture_url,is_following,tags,last_message_at",
            "line_user_id": f"eq.{uid}", "limit": "1"})
        user = u[0] if u else None
        slips_rows = await supa.select("slips", params={
            "select": "id,status,amount,expected_course,created_at,media_url", "line_user_id": f"eq.{uid}",
            "order": "created_at.desc", "limit": "10"})
        other_regs = await supa.select("registrations", params={
            "select": "id,course,paid,created_at", "line_user_id": f"eq.{uid}",
            "id": f"neq.{rid}", "limit": "10"})
    return {"registration": reg, "user": user, "slips": slips_rows, "other_registrations": other_regs}


@app.patch("/api/registrations/{rid}")
async def registration_update(rid: int, req: Request, admin=Depends(current_admin)):
    b = await req.json()
    patch = {k: b[k] for k in ("paid", "approved", "note", "name", "tel", "org", "email", "course") if k in b}
    patch["updated_at"] = NOW()
    await supa.update("registrations", patch, {"id": f"eq.{rid}"})
    # sync tag จ่ายแล้ว
    if b.get("paid") is True:
        r = await supa.select("registrations", params={"id": f"eq.{rid}", "select": "line_user_id", "limit": "1"})
        if r and r[0].get("line_user_id"):
            u = await supa.select("line_users", params={
                "select": "tags", "line_user_id": f"eq.{r[0]['line_user_id']}", "limit": "1"})
            cur = set((u[0].get("tags") if u else []) or [])
            if "จ่ายแล้ว" not in cur:
                await supa.update("line_users", {"tags": sorted(cur | {"จ่ายแล้ว"}), "updated_at": NOW()},
                                  {"line_user_id": f"eq.{r[0]['line_user_id']}"})
    return {"ok": True}


@app.post("/api/registrations/message")
async def registrations_message(req: Request, admin=Depends(current_admin)):
    """ส่งข้อความหาผู้ลงทะเบียนตาม filter (course / paid)"""
    b = await req.json()
    params = {"select": "line_user_id"}
    if b.get("course"):
        params["course"] = f"eq.{b['course']}"
    if b.get("paid") in ("true", "false", True, False):
        params["paid"] = f"eq.{str(b['paid']).lower()}"
    rows = await supa.select_all("registrations", params=params)
    uids = list({r["line_user_id"] for r in rows if r.get("line_user_id")})
    if not uids:
        raise HTTPException(400, "ไม่มีปลายทาง")
    msgs = _normalize_messages(b.get("messages", []))
    bid, msgs = await _broadcast_begin(admin["userId"], "multicast", msgs, len(uids))
    sent = failed = 0
    for i in range(0, len(uids), 500):
        code, _txt, _rid = await line.multicast(uids[i:i + 500], msgs)
        if code == 200:
            sent += len(uids[i:i + 500])
        else:
            failed += len(uids[i:i + 500])
    await _broadcast_finish(bid, status="sent" if failed == 0 else "failed")
    return {"ok": failed == 0, "target": len(uids), "sent": sent, "failed": failed}


@app.get("/api/settings")
async def get_settings(admin=Depends(current_admin)):
    rows = await supa.select("app_settings", params={"select": "*"})
    return {"settings": {r["key"]: r["value"] for r in rows}}


@app.post("/api/settings")
async def set_setting(req: Request, admin=Depends(current_admin)):
    b = await req.json()
    await supa.upsert("app_settings", {"key": b["key"], "value": b["value"], "updated_at": NOW()},
                      on_conflict="key")
    return {"ok": True}


@app.get("/api/admins")
async def list_admins(admin=Depends(current_admin)):
    return {"admins": await supa.select("admins", params={"select": "*", "order": "added_at"})}


@app.post("/api/admins")
async def add_admin(req: Request, admin=Depends(current_admin)):
    if admin.get("role") not in ("owner", "admin"):
        raise HTTPException(403, "ต้องเป็น owner/admin")
    b = await req.json()
    await supa.upsert("admins", {
        "line_user_id": b["lineUserId"], "name": b.get("name"), "role": b.get("role", "admin"),
    }, on_conflict="line_user_id")
    return {"ok": True}


@app.delete("/api/admins/{uid}")
async def del_admin(uid: str, admin=Depends(current_admin)):
    if admin.get("role") != "owner":
        raise HTTPException(403, "ต้องเป็น owner")
    await supa.delete("admins", {"line_user_id": f"eq.{uid}"})
    return {"ok": True}
