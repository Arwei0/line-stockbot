# -*- coding: utf-8 -*-
import time, math
from datetime import datetime, timezone, timedelta
import twstock
from twstock import realtime

TZ = timezone(timedelta(hours=8))

# ---- in-memory cache ----
_cache = {
    "rows": [],
    "by_code": {},
    "by_name": [],  # [(name_lower, row)]
    "updated_at": "",
}

BATCH = 50
RT_GAP = 0.8
HIS_GAP = 0.15
WEEK_DAYS = 25

def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def to_float(x):
    try:
        s = str(x).strip()
        if s in ("", "-", "—", "None", "null"): return None
        return float(s.replace(",", ""))
    except Exception:
        return None

def ma(values, n):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals: return None
    take = vals[-n:] if len(vals) >= n else vals
    return sum(take) / len(take)

def list_all():
    out = []
    for code, info in twstock.codes.items():
        if info.market in ("上市", "上櫃") and info.type == "股票" and code.isdigit():
            out.append((code, info.name, info.market))
    return out

def fetch_history_ma(code):
    try:
        st = twstock.Stock(code)
        closes = [to_float(x) for x in st.price][-30:]
        ma5d = ma(closes, 5)
        ma5w = ma(closes, WEEK_DAYS)
        # 取得最後一筆開/收，供離盤 fallback
        opens  = [to_float(x) for x in st.open]
        last_close = closes[-1] if closes else None
        last_open  = opens[-1]  if opens  else None
        return ma5d, ma5w, last_close, last_open
    except Exception:
        return None, None, None, None

def fetch_rt_batch(codes):
    data = {}
    try:
        resp = realtime.get(codes)
        if not resp or not resp.get("success"):
            return data
        for code, row in resp.get("realtime", {}).items():
            data[code] = {
                "price": to_float(row.get("latest_trade_price")),
                "open":  to_float(row.get("open")),
            }
    except Exception:
        pass
    return data

def rebuild_index(rows):
    by_code, by_name = {}, []
    for r in rows:
        code = r["symbol"]
        by_code[code] = r
        base = code.split(".")[0]
        by_code.setdefault(base, r)
        if r.get("name"):
            by_name.append((r["name"].lower(), r))
    return by_code, by_name

# ---- 對外：每日大重建（含 MA） ----
def build_snapshot_once():
    all_list = list_all()
    total = len(all_list)
    print(f"[init/daily] symbols: {total}")

    # 先把 MA 與最後一筆 OHLC 建好
    base = {}
    for i,(code,name,market) in enumerate(all_list,1):
        ma5d, ma5w, last_close, last_open = fetch_history_ma(code)
        base[code] = {"name":name, "market":market, "ma5_day":ma5d, "ma5_week":ma5w,
                      "price": last_close, "open": last_open}
        if i % 50 == 0:
            print(f"[MA] {i}/{total} ({i/total*100:.1f}%)")
        time.sleep(HIS_GAP)

    # 衝一次即時價
    rows = []
    stamp = now_str()
    batch_cnt = math.ceil(total / BATCH)
    for bi in range(batch_cnt):
        batch_codes = [c for c,_,_ in all_list[bi*BATCH:(bi+1)*BATCH]]
        rt = fetch_rt_batch(batch_codes)
        for c in batch_codes:
            b = base.get(c, {})
            price = (rt.get(c) or {}).get("price", b.get("price"))
            openp = (rt.get(c) or {}).get("open",  b.get("open"))
            rows.append({
                "symbol": f"{c}.TW",
                "name":   b.get("name") or c,
                "market": b.get("market") or "",
                "price":  price,
                "open":   openp,
                "ma5_day":  b.get("ma5_day"),
                "ma5_week": b.get("ma5_week"),
                "updated_at": stamp,
            })
        print(f"[RT] batch {bi+1}/{batch_cnt}")
        time.sleep(RT_GAP)

    by_code, by_name = rebuild_index(rows)
    _cache.update({
        "rows": rows,
        "by_code": by_code,
        "by_name": by_name,
        "updated_at": stamp,
    })
    print("[init/daily] snapshot ready:", len(rows))

# ---- 對外：只刷新即時價（不重算 MA） ----
def refresh_realtime_once():
    if not _cache["rows"]:
        build_snapshot_once()
        return
    # 取出代號
    codes = [r["symbol"].split(".")[0] for r in _cache["rows"]]
    total = len(codes)
    batch_cnt = math.ceil(total / BATCH)
    stamp = now_str()

    # 建索引
    base = {r["symbol"].split(".")[0]: r for r in _cache["rows"]}

    for bi in range(batch_cnt):
        batch_codes = codes[bi*BATCH:(bi+1)*BATCH]
        rt = fetch_rt_batch(batch_codes)
        for c in batch_codes:
            r = base.get(c)
            if not r: continue
            price = (rt.get(c) or {}).get("price") or r.get("price")
            openp = (rt.get(c) or {}).get("open")  or r.get("open")
            r["price"], r["open"], r["updated_at"] = price, openp, stamp
        print(f"[RT refresh] {bi+1}/{batch_cnt}")
        time.sleep(RT_GAP)

# ---- 提供給 app.py 查詢 ----
def get_cached_rows():
    return _cache["rows"], _cache["by_code"], _cache["by_name"], _cache["updated_at"]