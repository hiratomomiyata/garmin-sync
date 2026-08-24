#!/usr/bin/env python3
"""Garmin Connect -> Supabase 日次同期"""
import os, sys, json, time, datetime as dt
import requests
from garminconnect import Garmin

EMAIL = os.environ["GARMIN_EMAIL"]
PASSWORD = os.environ["GARMIN_PASSWORD"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TOKEN_DIR = os.environ.get("GARMIN_TOKEN_DIR", "/tmp/.garminconnect")
WALK_PACE = 540   # 9:00/km 以上は walk

H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
     "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}


def upsert(table, rows, conflict):
    if not rows:
        print(f"  {table}: 0件")
        return 0
    r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={conflict}",
                      headers=H, data=json.dumps(rows), timeout=60)
    if r.status_code >= 300:
        print(f"  !! {table}: HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
        return 0
    print(f"  {table}: {len(rows)}件")
    return len(rows)


def login():
    g = Garmin()
    try:
        mfa, _ = g.login(TOKEN_DIR)
        if mfa:
            raise RuntimeError("MFAが要求されました")
        print("既存トークンでログイン成功")
        return g
    except RuntimeError:
        raise
    except Exception as e:
        print(f"トークン再利用不可（{type(e).__name__}）")

    last = None
    for attempt in range(1, 4):
        try:
            g = Garmin(email=EMAIL, password=PASSWORD)
            mfa, _ = g.login(TOKEN_DIR)
            if mfa:
                raise RuntimeError("MFAが要求されました")
            print(f"新規ログイン成功（{attempt}回目）")
            return g
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            msg = str(e)
            print(f"ログイン失敗 {attempt}/3: {type(e).__name__}: {msg[:200]}", file=sys.stderr)
            if "429" in msg or "TooManyRequests" in msg:
                wait = 90 * attempt
                print(f"  レート制限。{wait}秒待機", file=sys.stderr)
                time.sleep(wait)
            else:
                time.sleep(15)
    raise SystemExit(f"ログイン不可: {last}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else 3
    print(f"=== 同期開始: 過去{days}日分 ===")
    g = login()
    today = dt.date.today()
    start = (today - dt.timedelta(days=days)).isoformat()
    loop_days = min(days, 45)
    dates = [today - dt.timedelta(days=i) for i in range(loop_days)]

     # --- body battery ---
    bb_rows = []
    for d in dates:
        try:
            b = g.get_body_battery(d.isoformat(), d.isoformat())
            if not (b and isinstance(b, list) and b[0].get("bodyBatteryValuesArray")):
                continue
            arr = b[0]["bodyBatteryValuesArray"]
            pts = []
            for x in arr:
                if len(x) < 2 or x[1] is None: continue
                ts = x[0]
                lv = x[-1] if isinstance(x[-1], (int, float)) and len(x) > 2 else x[1]
                if not isinstance(lv, (int, float)): continue
                pts.append((ts, int(lv)))
            if not pts: continue
            vals = [v for _, v in pts]
            # 起床帯(03:00-10:00 JST)の最大値を朝の値とする。無ければ日中最大
            morn = [v for ts, v in pts
                    if 3 <= ((ts/1000 + 9*3600) % 86400) / 3600 < 10]
            bb_morning = max(morn) if morn else max(vals)
            bb_rows.append({"measurement_date": d.isoformat(),
                            "bb_morning": bb_morning,
                            "bb_highest": max(vals),
                            "bb_lowest": min(vals),
                            "source": "garmin-api-auto"})
        except Exception as e:
            print(f"  bb {d}: {type(e).__name__}", file=sys.stderr)
    upsert("body_battery", bb_rows, "measurement_date")
