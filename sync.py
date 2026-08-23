#!/usr/bin/env python3
"""Garmin Connect -> Supabase 日次同期"""
import os, sys, json, datetime as dt
import requests
from garminconnect import Garmin

EMAIL = os.environ["GARMIN_EMAIL"]
PASSWORD = os.environ["GARMIN_PASSWORD"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TOKEN_DIR = os.environ.get("GARMIN_TOKEN_DIR", "/tmp/.garminconnect")

H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
     "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}

def upsert(table, rows, conflict):
    if not rows: return 0
    r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={conflict}",
                      headers=H, data=json.dumps(rows), timeout=30)
    if r.status_code >= 300:
        print(f"  ! {table}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return 0
    print(f"  {table}: {len(rows)}件")
    return len(rows)

def login():
    try:
        g = Garmin()
        g.login(TOKEN_DIR)
        print("トークンで再ログイン")
    except Exception:
        g = Garmin(EMAIL, PASSWORD)
        g.login()
        g.garth.dump(TOKEN_DIR)
        print("新規ログイン・トークン保存")
    return g

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    g = login()
    today = dt.date.today()
    dates = [today - dt.timedelta(days=i) for i in range(days)]

    ts_rows = []
    for d in dates:
        try:
            t = g.get_training_status(d.isoformat())
            mrs = (t or {}).get("mostRecentVO2Max") or {}
            vo2 = (mrs.get("generic") or {}).get("vo2MaxValue")
            tsm = (t or {}).get("mostRecentTrainingStatus") or {}
            status = None; load = None
            for v in (tsm.get("latestTrainingStatusData") or {}).values():
                status = v.get("trainingStatusFeedbackPhrase")
                load = v.get("loadTunnelMin")
            if vo2 or status:
                ts_rows.append({"measurement_date": d.isoformat(),
                                "vo2_max": vo2, "status": status,
                                "training_load": int(load) if load else None,
                                "notes": "garmin-api-auto"})
        except Exception as e:
            print(f"  ts {d}: {e}", file=sys.stderr)
    upsert("training_status", ts_rows, "measurement_date")

    bb_rows = []
    for d in dates:
        try:
            b = g.get_body_battery(d.isoformat(), d.isoformat())
            if b and isinstance(b, list) and b[0].get("bodyBatteryValuesArray"):
                vals = [x[1] for x in b[0]["bodyBatteryValuesArray"]
                        if len(x) > 1 and x[1] is not None]
                if vals:
                    bb_rows.append({"measurement_date": d.isoformat(),
                                    "bb_morning": vals[0], "bb_highest": max(vals),
                                    "bb_lowest": min(vals), "source": "garmin-api-auto"})
        except Exception as e:
            print(f"  bb {d}: {e}", file=sys.stderr)
    upsert("body_battery", bb_rows, "measurement_date")

    bm_rows = []
    try:
        start = (today - dt.timedelta(days=days)).isoformat()
        bc = g.get_body_composition(start, today.isoformat()) or {}
        for e in (bc.get("dateWeightList") or []):
            wt = e.get("weight")
            if not wt: continue
            ts = e.get("date") or e.get("calendarDate")
            if isinstance(ts, (int, float)):
                day = dt.datetime.utcfromtimestamp(ts/1000 + 9*3600).date().isoformat()
            else:
                day = str(ts)[:10]
            row = {"measurement_date": day, "weight_kg": round(wt/1000, 2)}
            if e.get("bodyFat") is not None:
                row["body_fat_pct"] = round(e["bodyFat"], 1)
            if e.get("bmi") is not None:
                row["bmi"] = round(e["bmi"], 1)
            bm_rows.append(row)
    except Exception as e:
        print(f"  body_composition: {e}", file=sys.stderr)
    upsert("body_measurements", bm_rows, "measurement_date")

    st_rows = []
    try:
        start = (today - dt.timedelta(days=days)).isoformat()
        for e in (g.get_daily_steps(start, today.isoformat()) or []):
            d = e.get("calendarDate"); tot = e.get("totalSteps")
            if not d or tot is None: continue
            st_rows.append({"measurement_date": d, "total_steps": tot,
                            "step_goal": e.get("stepGoal"),
                            "distance_m": e.get("totalDistance")})
    except Exception as e:
        print(f"  daily_steps: {e}", file=sys.stderr)
    upsert("daily_steps", st_rows, "measurement_date")

    sl_rows = []
    for d in dates:
        try:
            s = g.get_sleep_data(d.isoformat()) or {}
            dto = s.get("dailySleepDTO") or {}
            if dto.get("sleepTimeSeconds"):
                sl_rows.append({"measurement_date": d.isoformat(),
                    "duration_min": round(dto["sleepTimeSeconds"]/60),
                    "deep_min": round((dto.get("deepSleepSeconds") or 0)/60),
                    "light_min": round((dto.get("lightSleepSeconds") or 0)/60),
                    "rem_min": round((dto.get("remSleepSeconds") or 0)/60),
                    "awake_min": round((dto.get("awakeSleepSeconds") or 0)/60),
                    "sleep_score": ((dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
                    "resting_hr": s.get("restingHeartRate")})
        except Exception as e:
            print(f"  sleep {d}: {e}", file=sys.stderr)
    upsert("sleep_metrics", sl_rows, "measurement_date")

    try:
        acts = g.get_activities_by_date(
            (today - dt.timedelta(days=days)).isoformat(), today.isoformat(), "running")
        n = 0
        for a in acts or []:
            gid = a.get("activityId")
            if not gid: continue
            row = {
                "avg_cadence": round(a["averageRunningCadenceInStepsPerMinute"])
                    if a.get("averageRunningCadenceInStepsPerMinute") else None,
                "avg_stride_m": round(a["avgStrideLength"]/100, 2) if a.get("avgStrideLength") else None,
                "vertical_ratio": a.get("avgVerticalRatio"),
                "vertical_oscillation_cm": round(a["avgVerticalOscillation"]/10, 1)
                    if a.get("avgVerticalOscillation") else None,
                "ground_contact_ms": round(a["avgGroundContactTime"]) if a.get("avgGroundContactTime") else None,
                "gct_balance": a.get("avgGroundContactBalance"),
                "avg_power_w": round(a["avgPower"]) if a.get("avgPower") else None,
                "dynamics_source": "garmin-api-auto"}
            requests.patch(f"{SB_URL}/rest/v1/running_activities?garmin_activity_id=eq.{gid}",
                           headers=H, data=json.dumps(row), timeout=30)
            n += 1
        print(f"  running_activities: {n}件更新")
    except Exception as e:
        print(f"  activities: {e}", file=sys.stderr)

    print("完了")

if __name__ == "__main__":
    main()
