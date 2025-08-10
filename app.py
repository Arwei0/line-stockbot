# -*- coding: utf-8 -*-
import os, json, csv, time, threading, atexit
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import requests

# ---- 你的查價邏輯：from updater ----
# 這三個函式請確保在 updater.py 有提供
from updater import build_snapshot_once, refresh_realtime_once, get_cached_rows

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

TZ = timezone(timedelta(hours=8))  # Asia/Taipei
PORT = int(os.getenv("PORT", "5000"))

app = Flask(__name__)

# ---------- 共用小工具 ----------
def _fmt(x):
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "—"

def format_reply_full(row: dict):
    return "\n".join([
        "📊 台股即時/均線（雲端快取）",
        f"股票代號：{row['symbol']}",
        f"公司名稱：{row['name']}",
        f"現價：{_fmt(row.get('price'))}",
        f"開盤價：{_fmt(row.get('open'))}",
        f"五日均價：{_fmt(row.get('ma5_day'))}",
        f"五週均價：{_fmt(row.get('ma5_week'))}",
        f"更新時間：{row.get('updated_at','')}",
    ])

def build_quick_reply(suggestions):
    items = []
    for code, name in suggestions[:6]:
        items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": f"{code} {name}"[:20],
                "text": code
            }
        })
    return {"items": items} if items else None

def reply_message(reply_token: str, text: str, quick_reply=None):
    if not LINE_ACCESS_TOKEN:
        print("[DRYRUN]", text, "| quick_reply:", bool(quick_reply))
        return
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    msg = {"type": "text", "text": text[:4900]}
    if quick_reply:
        msg["quickReply"] = quick_reply
    data = {"replyToken": reply_token, "messages": [msg]}
    try:
        requests.post(LINE_REPLY_URL, headers=headers,
                      data=json.dumps(data), timeout=10)
    except Exception as e:
        print("[send error]", e)

# ---------- LINE webhook ----------
@app.post("/callback")
def callback():
    body = request.get_json(force=True, silent=True) or {}
    for ev in body.get("events", []):
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        reply_token = ev.get("replyToken", "")
        q = (msg.get("text") or "").strip()

        # 查找：先載入快取
        rows, by_code, by_name, file_time = get_cached_rows()
        if not rows:
            reply_message(reply_token, "資料還在準備中，稍後再試一次喔～")
            continue

        # 1) 代號精準
        row = by_code.get(q.upper()) or by_code.get(q.upper() + ".TW")
        if row:
            reply_message(reply_token, format_reply_full(row))
            continue

        # 2) 名稱模糊
        ql = q.lower()
        sugg = []
        for name_l, r in by_name:
            if ql in name_l:
                code = r["symbol"].split(".")[0]
                sugg.append((code, r["name"]))
                if len(sugg) >= 6:
                    break

        if len(sugg) == 1:
            code = sugg[0][0]
            row = by_code.get(code) or by_code.get(code + ".TW")
            reply_message(reply_token, format_reply_full(row))
        elif sugg:
            reply_message(reply_token, "找不到精準結果，您是不是要查：",
                          quick_reply=build_quick_reply(sugg))
        else:
            reply_message(reply_token, "查不到此股票。可輸入代號（如 2330）或公司名關鍵字（如 台積、聯發）。")
    return "OK"

# ---------- 健康檢查 ----------
@app.get("/healthz")
def health():
    rows, _, _, file_time = get_cached_rows()
    return {
        "rows": len(rows),
        "file_last_update": file_time,
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------- 內建排程（單機常駐 / Render 也適用） ----------
_running = True

def scheduler_loop():
    """
    每天：
      - 18:05 台北（10:05 UTC）做 daily 均線
    盤中：
      - 台北 09:00–13:35 每 10 分鐘做一次即時刷新
    其他時間：
      - 每整點做一次保底刷新
    """
    while _running:
        now_utc = datetime.now(timezone.utc)
        now_tpe = now_utc.astimezone(TZ)
        wd = now_tpe.weekday()  # 0=Mon

        try:
            # 每日 18:05 TPE 做 daily
            if wd <= 4 and now_tpe.strftime("%H:%M") == "18:05":
                print("[scheduler] daily rebuild…")
                build_snapshot_once()    # 重算 MA
                refresh_realtime_once()  # 補一次最新價
                time.sleep(60)

            # 盤中每 10 分鐘
            in_trading = wd <= 4 and ((9 <= now_tpe.hour < 13) or (now_tpe.hour == 13 and now_tpe.minute <= 35))
            if in_trading and (now_tpe.minute % 10 == 0):
                print("[scheduler] intraday 10-min refresh…", now_tpe)
                refresh_realtime_once()
                time.sleep(60)

            # 閒時每整點保底
            if now_tpe.minute == 0:
                print("[scheduler] hourly keep-warm refresh…", now_tpe)
                refresh_realtime_once()
                time.sleep(60)
        except Exception as e:
            print("[scheduler error]", e)

        time.sleep(10)

def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    return t

# ---------- 暖機（取代 before_first_request） ----------
def warmup():
    print("[warmup] building initial snapshot …")
    build_snapshot_once()
    refresh_realtime_once()
    start_scheduler()

_did_warmup = False
def _ensure_warmup_once():
    """避免重複暖機；在本機 __main__ 與 Render import 都會呼叫"""
    global _did_warmup
    if _did_warmup:
        return
    try:
        warmup()
    except Exception as e:
        print("[warmup error]", e)
    _did_warmup = True

# 在被 gunicorn --preload 匯入時也會先暖機一次
_ensure_warmup_once()

@atexit.register
def _shutdown():
    global _running
    _running = False

if __name__ == "__main__":
    _ensure_warmup_once()
    app.run(host="0.0.0.0", port=PORT)