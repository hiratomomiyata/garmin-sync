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

    # --- training status / VO2max ---
    ts_rows = []
    for d in dates:
        try:
            t = g.get_training_status(d.isoformat()) or {}
            mrs = t.get("mostRecentVO2Max") or {}
            vo2 = (mrs.get("generic") or {}).get("vo2MaxValue")
            tsm = t.get("mostRecentTrainingStatus") or {}
            status = None; load = None
            for v in (tsm.get("latestTrainingStatusData") or {}).values():
                status = v.get("trainingStatusFeedbackPhrase")
                load = v.get("loadTunnelMin")
            if vo2 or status:
                ts_rows.append({"measurement_date": d.isoformat(), "vo2_max": vo2,
                                "status": status,
                                "training_load": int(load) if load else None,
                                "notes": "garmin-api-auto"})
        except Exception as e:
            print(f"  ts {d}: {type(e).__name__}", file=sys.stderr)
    upsert("training_status", ts_rows, "measurement_date")

    # --- body battery（起床帯の最大値を朝の値とする）---
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
                lv = x[-1] if (len(x) > 2 and isinstance(x[-1], (int, float))) else x[1]
                if not isinstance(lv, (int, float)): continue
                pts.append((ts, int(lv)))
            if not pts: continue
            vals = [v for _, v in pts]
            morn = [v for ts, v in pts
                    if 3 <= ((ts/1000 + 9*3600) % 86400) / 3600 < 10]
            bb_rows.append({"measurement_date": d.isoformat(),
                            "bb_morning": max(morn) if morn else max(vals),
                            "bb_highest": max(vals),
                            "bb_lowest": min(vals),
                            "source": "garmin-api-auto"})
        except Exception as e:
            print(f"  bb {d}: {type(e).__name__}", file=sys.stderr)
    upsert("body_battery", bb_rows, "measurement_date")

    # --- body composition (体重・体脂肪) ---
    bm_rows = []
    try:
        bc = g.get_body_composition(start, today.isoformat()) or {}

        def to_day(v):
            if isinstance(v, (int, float)):
                return dt.datetime.utcfromtimestamp(v/1000 + 9*3600).date().isoformat()
            return str(v)[:10]

        def push(day, wt, fat=None, bmi=None):
            if not day or wt is None: return
            w = float(wt)
            if w > 300: w = w/1000.0
            row = {"measurement_date": day, "weight_kg": round(w, 2)}
            if fat is not None: row["body_fat_pct"] = round(float(fat), 1)
            if bmi is not None: row["bmi"] = round(float(bmi), 1)
            bm_rows.append(row)

        for e in (bc.get("dateWeightList") or []):
            push(to_day(e.get("calendarDate") or e.get("date")),
                 e.get("weight"), e.get("bodyFat"), e.get("bmi"))

        for s in (bc.get("dailyWeightSummaries") or []):
            day = to_day(s.get("summaryDate") or s.get("calendarDate"))
            lw = s.get("latestWeight") or {}
            mets = s.get("allWeightMetrics") or []
            src = lw if lw.get("weight") is not None else (mets[-1] if mets else {})
            push(day, src.get("weight"), src.get("bodyFat"), src.get("bmi"))

        if not bm_rows:
            print(f"  body_comp: 該当なし keys={list(bc.keys())[:6]}", file=sys.stderr)
    except Exception as e:
        print(f"  body_comp: {type(e).__name__}: {e}", file=sys.stderr)
    upsert("body_measurements", bm_rows, "measurement_date")

    # --- daily steps ---
    st_rows = []
    try:
        for e in (g.get_daily_steps(start, today.isoformat()) or []):
            d = e.get("calendarDate"); tot = e.get("totalSteps")
            if not d or tot is None: continue
            st_rows.append({"measurement_date": d, "total_steps": int(tot),
                            "step_goal": e.get("stepGoal"),
                            "distance_m": int(e["totalDistance"]) if e.get("totalDistance") else None})
    except Exception as e:
        print(f"  steps: {type(e).__name__}: {e}", file=sys.stderr)
    upsert("daily_steps", st_rows, "measurement_date")

    # --- sleep ---
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
            print(f"  sleep {d}: {type(e).__name__}", file=sys.stderr)
    upsert("sleep_metrics", sl_rows, "measurement_date")

    # --- activities (run / walk を判別して登録) ---
    try:
        acts = g.get_activities_by_date(start, today.isoformat(), "running") or []
        nrun = 0; nwalk = 0
        for a in acts:
            gid = a.get("activityId")
            if not gid: continue
            dist = a.get("distance"); dur = a.get("duration")
            if not dist or not dur: continue
            km = round(dist/1000.0, 2)
            pace = round(dur/(dist/1000.0))
            atype = "walk" if pace >= WALK_PACE else "run"
            cad = a.get("averageRunningCadenceInStepsPerMinute")
            st = a.get("startTimeGMT")
            row = {
                "garmin_activity_id": str(gid),
                "activity_date": (st.replace(" ", "T") + "+00:00") if st else None,
                "activity_type": atype,
                "distance_km": km,
                "duration_seconds": round(dur),
                "avg_pace_sec_per_km": pace,
                "avg_heart_rate": round(a["averageHR"]) if a.get("averageHR") else None,
                "max_heart_rate": round(a["maxHR"]) if a.get("maxHR") else None,
                "calories": round(a["calories"]) if a.get("calories") else None,
                "elevation_gain_m": round(a["elevationGain"], 1) if a.get("elevationGain") else None,
                "course_name": a.get("activityName"),
                "avg_cadence": round(cad) if cad else None,
                "avg_stride_m": round(a["avgStrideLength"]/100, 2) if a.get("avgStrideLength") else None,
                "vertical_ratio": a.get("avgVerticalRatio"),
                "vertical_oscillation_cm": round(a["avgVerticalOscillation"]/10, 1) if a.get("avgVerticalOscillation") else None,
                "ground_contact_ms": round(a["avgGroundContactTime"]) if a.get("avgGroundContactTime") else None,
                "avg_power_w": round(a["avgPower"]) if a.get("avgPower") else None,
                "training_load": a.get("activityTrainingLoad"),
                "vo2_max": a.get("vO2MaxValue"),
                "dynamics_source": "garmin-api-auto"}
            row = {k: v for k, v in row.items() if v is not None}
            r = requests.post(f"{SB_URL}/rest/v1/running_activities?on_conflict=garmin_activity_id",
                              headers=H, data=json.dumps([row]), timeout=30)
            if r.status_code < 300:
                if atype == "run": nrun += 1
                else: nwalk += 1
            else:
                print(f"  !! act {gid}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        print(f"  activities: run {nrun}件 / walk {nwalk}件（全{len(acts)}件）")
    except Exception as e:
        print(f"  activities: {type(e).__name__}: {e}", file=sys.stderr)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
