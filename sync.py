#!/usr/bin/env python3
import os, sys, json, time, datetime as dt
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
            print(f"  bb {d}: {type(e).__name__}", file=sys.stderr)
    upsert("body_battery", bb_rows, "measurement_date")

    bm_rows = []
    try:
        bc = g.get_body_composition(start, today.isoformat()) or {}
        for e in (bc.get("dateWeightList") or []):
            wt = e.get("weight")
            if not wt: continue
            ts = e.get("calendarDate") or e.get("date")
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
        print(f"  body_comp: {type(e).__name__}: {e}", file=sys.stderr)
    upsert("body_measurements", bm_rows, "measurement_date")

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

    try:
        acts = g.get_activities_by_date(start, today.isoformat(), "running") or []
        n = 0
        for a in acts:
            gid = a.get("activityId")
            if not gid: continue
            cad = a.get("averageRunningCadenceInStepsPerMinute")
            row = {
                "avg_cadence": round(cad) if cad else None,
                "avg_stride_m": round(a["avgStrideLength"]/100, 2) if a.get("avgStrideLength") else None,
                "vertical_ratio": a.get("avgVerticalRatio"),
                "vertical_oscillation_cm": round(a["avgVerticalOscillation"]/10, 1)
                    if a.get("avgVerticalOscillation") else None,
                "ground_contact_ms": round(a["avgGroundContactTime"]) if a.get("avgGroundContactTime") else None,
                "gct_balance": a.get("avgGroundContactBalance"),
                "avg_power_w": round(a["avgPower"]) if a.get("avgPower") else None,
                "dynamics_source": "garmin-api-auto"}
            row = {k: v for k, v in row.items() if v is not None}
            if len(row) <= 1: continue
            r = requests.patch(
                f"{SB_URL}/rest/v1/running_activities?garmin_activity_id=eq.{gid}",
                headers=H, data=json.dumps(row), timeout=30)
            if r.status_code < 300:
                n += 1
        print(f"  running_activities: {len(acts)}件中 {n}件更新")
    except Exception as e:
        print(f"  activities: {type(e).__name__}: {e}", file=sys.stderr)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
