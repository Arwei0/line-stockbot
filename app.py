# -*- coding: utf-8 -*-
import os, json, csv, time, threading, atexit
from datetime import datetime, timedelta, timezone
from flask import Flask, request
import requests

# ---- 你的查價邏輯：用 updater 裡的功能 ----
# 我把你原本 updater.py 的三個函式抽成可呼叫方法（見下方附註）
from updater import build_snapshot_once, refresh_realtime_once, get_cached_rows

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

TZ = timezone(timedelta(hours=8))  # Asia/Taipei
PORT = int(os.getenv("PORT", "5000"))

app = Flask(__name__)

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
        items.append({"type":"action","action":{"type":"message","label":f"{code} {name}"[:20],"text":code}})
    return {"items": items} if items else None

def reply_message(reply_token: str, text: str, quick_reply=None):
    if not LINE_ACCESS_TOKEN:
        print("[DRYRUN]", text)
        return
    headers = {"Content-Type":"application/json","Authorization":f"Bearer {LINE_ACCESS_TOKEN}"}
    msg = {"type":"text","text": text[:4900]}
    if quick_reply: msg["quickReply"] = quick_reply
    data = {"replyToken": reply_token, "messages": [msg]}
    try:
        requests.post(LINE_REPLY_URL, headers=headers, data=json.dumps(data), timeout=10)
    except Exception as e:
        print("[send error]", e)

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

        # 1) 代號
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
                if len(sugg) >= 6: break

        if len(sugg) == 1:
            code = sugg[0][0]
            row = by_code.get(code) or by_code.get(code + ".TW")
            reply_message(reply_token, format_reply_full(row))
        elif sugg:
            reply_message(reply_token, "找不到精準結果，您是不是要查：", quick_reply=build_quick_reply(sugg))
        else:
            reply_message(reply_token, "查不到此股票。可輸入代號（如 2330）或公司名關鍵字（如 台積、聯發）。")
    return "OK"

@app.get("/healthz")
def health():
    rows, _, _, file_time = get_cached_rows()
    return {
        "rows": len(rows),
        "file_last_update": file_time,
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------- 內建排程（單機執行，不需要 Render Cron） ----------
_running = True
def scheduler_loop():
    """
    每天：
      - 10:05 UTC（18:05 台北）做 daily 均線
    盤中：
      - 01:00-05:35 UTC（台北 09:00-13:35）每 10 分鐘做一次即時刷新
    其他時間：
      - 每 60 分鐘做一次保底刷新
    """
    while _running:
        now_utc = datetime.now(timezone.utc)
        now_tpe = now_utc.astimezone(TZ)
        wd = now_tpe.weekday()  # 0=Mon

        try:
            # 每日 18:05 台北（10:05 UTC）跑一次 daily
            if now_tpe.strftime("%H:%M") == "18:05" and wd <= 4:
                print("[scheduler] daily rebuld…")
                build_snapshot_once()   # 重算 MA
                refresh_realtime_once() # 補一次最新價
                time.sleep(60)

            # 盤中每 10 分鐘（台北 09:00-13:35）
            in_trading = wd <= 4 and ((9 <= now_tpe.hour <= 13) or (now_tpe.hour == 13 and now_tpe.minute <= 35))
            if in_trading and now_tpe.minute % 10 == 0:
                print("[scheduler] intraday 10-min refresh…", now_tpe)
                refresh_realtime_once()
                time.sleep(60)

            # 閒時每 60 分鐘保底
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

@app.before_first_request
def warmup():
    # 第一次啟動：先建快照一次（避免空表）
    print("[warmup] building initial snapshot …")
    build_snapshot_once()
    refresh_realtime_once()
    start_scheduler()

@atexit.register
def _shutdown():
    global _running
    _running = False

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)